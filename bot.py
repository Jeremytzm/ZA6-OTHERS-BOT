"""
Telegram Points Bot
--------------------
Tracks two teams' points from group outings, including bonus points and
shared reflections for outings with spiritual/significant impact, plus a
weekly Sunday team-points summary.

See README.md for setup instructions.
"""

import logging
import os
import re
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

def _env_or_default(key: str, default: str) -> str:
    """os.getenv's default only kicks in when the var is unset, not when a
    hosting platform sets it to an empty string — treat blank the same as unset."""
    value = os.getenv(key)
    return value if value else default


BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = _env_or_default("TIMEZONE", "Asia/Singapore")
SUMMARY_HOUR = int(_env_or_default("SUMMARY_HOUR", "18"))  # 24h, local to TIMEZONE

OUTING_POINTS = 3
IMPACT_POINTS = 3
SHARE_POINTS = 4
NEW_GROUND_POINTS = 30
IMPACT_LOG_POINTS = 5  # flat reward for a standalone Impact log (no description step)

(
    SELECT_ATTENDEES,
    SELECT_IMPACT,
    LOG_TYPE_CHOICE,
    DM_ASK_DESCRIPTION,
    DM_SELECT_TEAMMATES,
    DM_ASK_IMPACT,
    DM_ASK_SHARE,
    DM_AWAIT_STORY,
    DM_ASK_PHOTO,
    DM_AWAIT_PHOTO,
    IMPACT_AWAIT_STORY,
    IMPACT_ASK_PHOTO,
    IMPACT_AWAIT_PHOTO,
    NG_AWAIT_DESCRIPTION,
    NG_AWAIT_STORY,
    NG_ASK_PHOTO,
    NG_AWAIT_PHOTO,
) = range(17)

GROUP_CAPTION_LIMIT = 1024  # Telegram's hard limit on photo captions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Restrict sensitive commands to Telegram group admins/creator."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


def display_name_of(user) -> str:
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


# ---------------------------------------------------------------------------
# Passive tracking: learn user_ids for pre-registered usernames, and keep
# member identity info fresh, whenever anyone posts in the group.
# ---------------------------------------------------------------------------

async def track_group_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    user = update.effective_user
    name = display_name_of(user)

    resolved = db.resolve_pending_member_if_any(user.id, user.username, name)
    if resolved:
        member = db.get_member(user.id)
        await update.effective_chat.send_message(
            f"👋 Welcome {name}! You've been added to {db.team_display_name(member['team'])}."
        )
        return

    if db.get_member(user.id):
        db.sync_member_identity(user.id, user.username, name)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

START_BUTTON_TEXT = "✅ Start"


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛟Log an outing/impact", callback_data="menu:log")],
            [InlineKeyboardButton("Took new grounds ❤️‍🔥", callback_data="menu:newground")],
            [InlineKeyboardButton("📊 View points & outings", callback_data="menu:view")],
        ]
    )


def build_persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(START_BUTTON_TEXT)]], resize_keyboard=True)


async def send_start_menu(message, user, context: ContextTypes.DEFAULT_TYPE) -> None:
    outing_id = db.get_pending_share(user.id)
    if outing_id is not None:
        await message.reply_text(
            "Hey! Would you like to share a bit about how that outing went? "
            "Just reply to me here with your thoughts and I'll post it to the group "
            f"(+{SHARE_POINTS} bonus points if you do). No pressure if you'd rather not.",
            reply_markup=build_persistent_keyboard(),
        )
        return
    await message.reply_text("Hi! What would you like to do?", reply_markup=build_main_menu_keyboard())
    await message.reply_text(
        "⌨️ Tip: tap Start below anytime to bring this menu back up — even mid-way through "
        "logging an outing.",
        reply_markup=build_persistent_keyboard(),
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == ChatType.PRIVATE:
        await send_start_menu(update.message, user, context)
    else:
        await help_cmd(update, context)


async def restart_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_start_menu(update.message, update.effective_user, context)
    return ConversationHandler.END


async def menu_view_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # No parse_mode here: outing descriptions are free-form user text and could
    # contain stray Markdown characters that would break entity parsing.
    text = build_summary_text() + "\n\n" + build_outings_list_text()
    await query.edit_message_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Points Bot — commands*\n\n"
        "_Setup (group admins only):_\n"
        "/setgroup — register this chat as the group for announcements & weekly summary\n"
        "/addmember @handle team1|team2 — add/move a member (or reply to their message with "
        "/addmember team1|team2)\n"
        "/removemember @handle — remove a member\n"
        "/listmembers — list all members and their teams\n"
        "/cleardata — wipe all outings & points history (keeps members/settings), asks to confirm\n\n"
        "_Logging outings (admins only, in the group):_\n"
        "/logouting <description> — start the guided flow to log who attended\n\n"
        "_Logging your own outing or impact (DM me directly):_\n"
        "Send /start and tap *🛟Log an outing/impact* to choose between:\n"
        "  • *Outing* (or /logouting <description>) — I'll ask which teammates joined you (they "
        "earn points too), whether it had a spiritual/significant impact, then give you the "
        "option to share a short story and a photo (as separate steps), and post it all to the "
        "group with everyone's points earned.\n"
        f"  • *Impact* (or /logimpact) — for when you made an impact (e.g. loving/praying for "
        f"someone) without a full outing. Share a bit about it, optionally attach a photo, and "
        f"earn a flat +{IMPACT_LOG_POINTS} pts. No teammates can be added to an impact.\n\n"
        "Tap *📊 View points & outings* from the same menu to see team totals and recent outings. "
        "The ✅ Start button (below the message box) brings the menu back up anytime — even "
        "mid-way through logging an outing.\n\n"
        "_Logging new grounds taken (DM me directly):_\n"
        "Tap *Took new grounds ❤️‍🔥* from the /start menu, tell us about it, share a bit more "
        f"detail, optionally attach a photo, and earn +{NEW_GROUND_POINTS} pts posted straight "
        "to the group.\n\n"
        "_Everyone:_\n"
        "/points — show current team point totals\n"
        "/help — show this message"
    )
    await update.effective_chat.send_message(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /setgroup
# ---------------------------------------------------------------------------

async def setgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        await update.message.reply_text("Only group admins can do this.")
        return
    db.set_setting("group_chat_id", str(update.effective_chat.id))
    await update.message.reply_text(
        "✅ This chat is now registered for outing announcements and the weekly points summary."
    )


# ---------------------------------------------------------------------------
# /addmember, /removemember, /listmembers
# ---------------------------------------------------------------------------

async def addmember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        await update.message.reply_text("Only group admins can do this.")
        return

    args = context.args
    reply = update.message.reply_to_message

    if reply and args:
        team = db.normalize_team(args[0])
        if not team:
            await update.message.reply_text("Team must be `team1` or `team2`.", parse_mode="Markdown")
            return
        target = reply.from_user
        name = display_name_of(target)
        db.add_or_update_member(target.id, target.username, name, team)
        await update.message.reply_text(f"✅ {name} added to {db.team_display_name(team)}.")
        return

    if len(args) >= 2:
        username = args[0]
        team = db.normalize_team(args[1])
        if not team:
            await update.message.reply_text("Team must be `team1` or `team2`.", parse_mode="Markdown")
            return

        existing = db.find_member_by_username(username)
        if existing:
            db.add_or_update_member(existing["user_id"], existing["username"], existing["display_name"], team)
            await update.message.reply_text(
                f"✅ {existing['display_name']} moved to {db.team_display_name(team)}."
            )
            return

        db.add_pending_member(username, team)
        await update.message.reply_text(
            f"📝 Noted — @{username.lstrip('@')} will be added to {db.team_display_name(team)} "
            "as soon as they next post in this group (Telegram doesn't let bots look up users "
            "who haven't sent a message yet). Alternatively, reply to one of their messages with "
            "`/addmember team1` or `/addmember team2`.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "Usage: `/addmember @handle team1` (or reply to their message with `/addmember team1`)",
        parse_mode="Markdown",
    )


async def removemember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        await update.message.reply_text("Only group admins can do this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/removemember @handle`", parse_mode="Markdown")
        return
    removed = db.remove_member(context.args[0])
    await update.message.reply_text("✅ Removed." if removed else "Couldn't find that member.")


async def listmembers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    members = db.get_all_members()
    if not members:
        await update.message.reply_text("No members registered yet.")
        return
    by_team = {"team1": [], "team2": []}
    for m in members:
        by_team.setdefault(m["team"], []).append(m["display_name"])
    lines = []
    for team in ("team1", "team2"):
        lines.append(f"*{db.team_display_name(team)}*")
        names = by_team.get(team, [])
        if names:
            lines.extend(f"  • {n}" for n in names)
        else:
            lines.append("  (none)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /cleardata (admin-only, with confirmation)
# ---------------------------------------------------------------------------

async def cleardata_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        await update.message.reply_text("Only group admins can do this.")
        return
    await update.message.reply_text(
        "⚠️ This will wipe all outings and points history for *everyone* "
        "(members and the group settings stay intact). This can't be undone. "
        "Are you sure?",
        parse_mode="Markdown",
        reply_markup=build_yes_no_keyboard("cleardata"),
    )


async def cleardata_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_group_admin(update, context):
        await query.edit_message_text("Only group admins can do this.")
        return

    if query.data == "cleardata:yes":
        db.clear_points_data()
        await query.edit_message_text("✅ Cleared. All outings and points are back to 0.")
    else:
        await query.edit_message_text("Cancelled — nothing was cleared.")


# ---------------------------------------------------------------------------
# /points
# ---------------------------------------------------------------------------

async def points_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(build_summary_text())


def build_summary_text() -> str:
    totals = db.team_totals()
    t1, t2 = totals["team1"], totals["team2"]
    lines = ["📊 *Current Team Points*", ""]
    lines.append(f"{db.team_display_name('team1')}: {t1} pts")
    lines.append(f"{db.team_display_name('team2')}: {t2} pts")
    if t1 != t2:
        leader = "team1" if t1 > t2 else "team2"
        lines.append(f"\n🏆 {db.team_display_name(leader)} is currently in the lead!")
    else:
        lines.append("\n🤝 It's currently tied!")
    return "\n".join(lines)


OUTINGS_LIST_LIMIT = 10


def format_outing_line(row) -> str:
    try:
        date_str = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%b %d")
    except (ValueError, TypeError):
        date_str = row["created_at"] or "?"
    participants = row["participants"] or "—"
    return f"🗓 {date_str} — \"{row['description']}\" — {row['total_points']} pts ({participants})"


def build_outings_list_text() -> str:
    outings = db.list_outings(limit=OUTINGS_LIST_LIMIT)
    if not outings:
        return "📜 *Past Outings*\n\nNo outings logged yet."
    lines = [f"📜 *Past Outings* (most recent {len(outings)})", ""]
    lines.extend(format_outing_line(o) for o in outings)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /logouting conversation
# ---------------------------------------------------------------------------

def build_attendee_keyboard(members, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for m in members:
        mark = "✅" if m["user_id"] in selected else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {m['display_name']}", callback_data=f"att:{m['user_id']}")])
    rows.append([InlineKeyboardButton("▶️ Done selecting", callback_data="att:done")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="att:cancel")])
    return InlineKeyboardMarkup(rows)


def build_impact_keyboard(members, selected_ids, impact: set) -> InlineKeyboardMarkup:
    rows = []
    for m in members:
        if m["user_id"] not in selected_ids:
            continue
        mark = "✅" if m["user_id"] in impact else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {m['display_name']}", callback_data=f"imp:{m['user_id']}")])
    rows.append([InlineKeyboardButton("🏁 Finish", callback_data="imp:done")])
    return InlineKeyboardMarkup(rows)


async def logouting_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, context):
        await update.message.reply_text("Only group admins can log outings.")
        return ConversationHandler.END

    members = db.get_all_members()
    if not members:
        await update.message.reply_text("No members registered yet — add some with /addmember first.")
        return ConversationHandler.END

    description = " ".join(context.args) if context.args else "Outing"
    context.chat_data["outing_desc"] = description
    context.chat_data["selected"] = set()

    await update.message.reply_text(
        f"Logging outing: *{description}*\nTap everyone who attended, then tap Done.",
        parse_mode="Markdown",
        reply_markup=build_attendee_keyboard(members, set()),
    )
    return SELECT_ATTENDEES


async def attendee_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "att:cancel":
        context.chat_data.clear()
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    members = db.get_all_members()
    selected: set = context.chat_data.get("selected", set())

    if data == "att:done":
        if not selected:
            await query.answer("Select at least one attendee first.", show_alert=True)
            return SELECT_ATTENDEES
        context.chat_data["selected"] = selected
        context.chat_data["impact"] = set()
        await query.edit_message_text(
            "Now select who experienced a spiritual/significant impact (or tap Finish if none did):",
            reply_markup=build_impact_keyboard(members, selected, set()),
        )
        return SELECT_IMPACT

    user_id = int(data.split(":")[1])
    if user_id in selected:
        selected.discard(user_id)
    else:
        selected.add(user_id)
    context.chat_data["selected"] = selected

    await query.edit_message_reply_markup(reply_markup=build_attendee_keyboard(members, selected))
    return SELECT_ATTENDEES


async def impact_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    members = db.get_all_members()
    selected = context.chat_data.get("selected", set())
    impact: set = context.chat_data.get("impact", set())

    if data == "imp:done":
        await query.edit_message_text("Processing outing...")
        await finalize_outing(update, context, members, selected, impact)
        context.chat_data.clear()
        return ConversationHandler.END

    user_id = int(data.split(":")[1])
    if user_id in impact:
        impact.discard(user_id)
    else:
        impact.add(user_id)
    context.chat_data["impact"] = impact

    await query.edit_message_reply_markup(reply_markup=build_impact_keyboard(members, selected, impact))
    return SELECT_IMPACT


async def logouting_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def finalize_outing(update, context, members, selected: set, impact: set):
    description = context.chat_data.get("outing_desc", "Outing")
    chat = update.effective_chat
    outing_id = db.create_outing(description)
    by_id = {m["user_id"]: m for m in members}

    for user_id in selected:
        member = by_id.get(user_id)
        if not member:
            continue
        team = member["team"]
        name = member["display_name"]

        db.log_points(user_id, team, OUTING_POINTS, "outing_participation", outing_id)
        points_earned = OUTING_POINTS
        msg = f"🙌 {name} went on the outing \"{description}\" and earned +{OUTING_POINTS} pts for {db.team_display_name(team)}!"

        had_impact = user_id in impact
        if had_impact:
            db.log_points(user_id, team, IMPACT_POINTS, "spiritual_impact", outing_id)
            points_earned += IMPACT_POINTS
            msg += f"\n✨ There was also a spiritual/significant impact — +{IMPACT_POINTS} more pts!"

        msg += f"\n(Total earned this outing: {points_earned} pts)"
        await chat.send_message(msg)

        if had_impact:
            db.set_pending_share(user_id, outing_id)
            prompt = (
                f"Hey {name}! I saw the outing \"{description}\" had a spiritual/significant impact on you. "
                f"Want to share a bit more about it? If you reply here, I'll post it to the group "
                f"and you'll earn +{SHARE_POINTS} bonus points. Totally optional!"
            )
            try:
                await context.bot.send_message(chat_id=user_id, text=prompt)
            except Forbidden:
                mention = f"@{member['username']}" if member["username"] else name
                await chat.send_message(
                    f"💌 {mention}, I'd love to hear more about your outing, but I can't message you "
                    f"privately yet — please tap Start on me (search for the bot and hit Start), "
                    f"then I'll ask you there!"
                )


# ---------------------------------------------------------------------------
# /logouting via DM: a member logs their own outing directly to the bot
# ---------------------------------------------------------------------------

def build_dm_impact_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✨ Yes", callback_data="dmimp:yes"),
                InlineKeyboardButton("No", callback_data="dmimp:no"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="dmimp:cancel")],
        ]
    )


def build_log_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛟 Outing", callback_data="logtype:outing")],
            [InlineKeyboardButton("❤️ Impact", callback_data="logtype:impact")],
        ]
    )


def build_yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data=f"{prefix}:yes"), InlineKeyboardButton("No thanks", callback_data=f"{prefix}:no")]]
    )


def build_teammates_keyboard(members, self_id: int, team: str, selected: set) -> InlineKeyboardMarkup:
    rows = []
    for m in members:
        if m["user_id"] == self_id:
            continue
        mark = "✅" if m["user_id"] in selected else "⬜"
        label = m["display_name"]
        if m["team"] != team:
            label += f" ({db.team_display_name(m['team'])})"
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"dmteam:{m['user_id']}")])
    rows.append([InlineKeyboardButton("▶️ Done", callback_data="dmteam:done")])
    return InlineKeyboardMarkup(rows)


async def send_teammates_prompt(message, description: str, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["dm_outing_desc"] = description
    context.user_data["teammates_selected"] = set()
    user = message.from_user
    member = db.get_member(user.id)
    members = db.get_all_members()
    await message.reply_text(
        "Let's goooo 🔥 Select anyone else who joined (from either team), then tap Done (just tap "
        "Done if it was just you).",
        reply_markup=build_teammates_keyboard(members, user.id, member["team"], set()),
    )
    return DM_SELECT_TEAMMATES


async def logouting_dm_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = db.get_member(user.id)
    if not member:
        await update.message.reply_text(
            "You're not registered as a member yet — ask a group admin to add you with "
            "/addmember first, then try again."
        )
        return ConversationHandler.END

    description = " ".join(context.args) if context.args else "Outing"
    return await send_teammates_prompt(update.message, description, context)


async def logouting_menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    member = db.get_member(user.id)
    if not member:
        await query.edit_message_text(
            "You're not registered as a member yet — ask a group admin to add you with "
            "/addmember first, then try again."
        )
        return ConversationHandler.END

    context.user_data.clear()
    await query.edit_message_text(
        "What would you like to log?",
        reply_markup=build_log_type_keyboard(),
    )
    return LOG_TYPE_CHOICE


async def log_type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "logtype:outing":
        await query.edit_message_text("Tell us more about it\nI.E Dinner with Kesle 👀")
        return DM_ASK_DESCRIPTION

    await query.edit_message_text("Share more about the impact you made ❤️‍🔥")
    return IMPACT_AWAIT_STORY


async def dm_description_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == START_BUTTON_TEXT:
        return await restart_button_handler(update, context)
    description = update.message.text.strip() or "Outing"
    return await send_teammates_prompt(update.message, description, context)


async def dm_team_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "dmteam:done":
        await query.edit_message_text(
            "Did you make a significant/spiritual impact?",
            reply_markup=build_dm_impact_keyboard(),
        )
        return DM_ASK_IMPACT

    teammate_id = int(data.split(":")[1])
    selected: set = context.user_data.get("teammates_selected", set())
    if teammate_id in selected:
        selected.discard(teammate_id)
    else:
        selected.add(teammate_id)
    context.user_data["teammates_selected"] = selected

    user = update.effective_user
    member = db.get_member(user.id)
    members = db.get_all_members()
    await query.edit_message_reply_markup(
        reply_markup=build_teammates_keyboard(members, user.id, member["team"], selected)
    )
    return DM_SELECT_TEAMMATES


async def dm_impact_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "dmimp:cancel":
        context.user_data.clear()
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    had_impact = data == "dmimp:yes"
    context.user_data["had_impact"] = had_impact

    if had_impact:
        await query.edit_message_text(
            "OH CMON NOW ❤️‍🔥 Want to share a short story about it? You'll earn "
            f"+{SHARE_POINTS} bonus pts if you do!",
            reply_markup=build_yes_no_keyboard("dmshare"),
        )
        return DM_ASK_SHARE

    await query.edit_message_text("Processing...")
    await finalize_dm_outing(update, context)
    return ConversationHandler.END


async def dm_share_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "dmshare:no":
        await query.edit_message_text("Processing...")
        await finalize_dm_outing(update, context)
        return ConversationHandler.END

    await query.edit_message_text("Share more about how it went!")
    return DM_AWAIT_STORY


async def dm_story_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.text == START_BUTTON_TEXT:
        return await restart_button_handler(update, context)
    if message.photo:
        await message.reply_text(
            "Let's get the story in words first — send it as a text message, and I'll ask "
            "for a photo separately right after."
        )
        return DM_AWAIT_STORY

    context.user_data["story_text"] = message.text
    await message.reply_text(
        "Got it! Want to attach a photo too?",
        reply_markup=build_yes_no_keyboard("dmphoto"),
    )
    return DM_ASK_PHOTO


async def dm_photo_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "dmphoto:no":
        await query.edit_message_text("Processing...")
        await finalize_dm_outing(update, context)
        return ConversationHandler.END

    await query.edit_message_text("Feel free to send me the photo 😁")
    return DM_AWAIT_PHOTO


async def dm_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    context.user_data["story_photo"] = message.photo[-1].file_id
    await message.reply_text("Got it! Logging your outing now...")
    await finalize_dm_outing(update, context)
    return ConversationHandler.END


def build_dm_outing_header(
    name: str,
    team_disp: str,
    description: str,
    had_impact: bool,
    shared: bool,
    teammate_names: list[str],
) -> tuple[str, int]:
    """Builds the announcement text *excluding* the story and team totals, so
    callers can slot the story between the two (and fit it into a length
    budget, e.g. Telegram's 1024-char photo caption cap).

    The reporter earns OUTING_POINTS for themselves, plus another OUTING_POINTS
    for each teammate who joined them (a "company bonus"); each teammate also
    separately earns their own OUTING_POINTS for attending (logged elsewhere)."""
    lines = [f"🙌 {name} went on an outing \"{description}\" and earned +{OUTING_POINTS} pts for {team_disp}!"]
    points_earned = OUTING_POINTS

    if teammate_names:
        company_bonus = OUTING_POINTS * len(teammate_names)
        points_earned += company_bonus
        lines.append(f"👥 Also joined: {', '.join(teammate_names)} (+{OUTING_POINTS} pts each)")

    if had_impact:
        points_earned += IMPACT_POINTS
        lines.append(f"✨ There was also a spiritual/significant impact — +{IMPACT_POINTS} more pts!")

    if shared:
        points_earned += SHARE_POINTS

    # The displayed total covers everyone credited from this outing (reporter +
    # teammates), not just the reporter's own share, which is what's returned below.
    grand_total = points_earned + OUTING_POINTS * len(teammate_names)
    lines.append(f"(Total earned this outing: {grand_total} pts)")

    return "\n".join(lines), points_earned


def team_totals_lines() -> list[str]:
    totals = db.team_totals()
    t1, t2 = totals["team1"], totals["team2"]
    t1_disp, t2_disp = db.team_display_name("team1"), db.team_display_name("team2")
    lines = [f"📊 {t1_disp}: {t1} pts | {t2_disp}: {t2} pts"]
    if t1 != t2:
        leader_disp = t1_disp if t1 > t2 else t2_disp
        lines.append(f"🏆 {leader_disp} is currently in the lead!")
    else:
        lines.append("🤝 It's currently tied!")
    return lines


def build_group_message(header: str, story_text: str | None) -> str:
    """Group announcements read: header (points earned), then the shared
    story, then team totals."""
    totals = "\n".join(team_totals_lines())
    parts = [header]
    if story_text:
        parts.append(story_text)
    parts.append(totals)
    return "\n\n".join(parts)


def build_group_caption(header: str, story_text: str | None) -> str:
    """Same layout as build_group_message, but trims the story (not the
    header or team totals) to fit Telegram's 1024-char photo caption cap."""
    totals = "\n".join(team_totals_lines())
    if not story_text:
        return f"{header}\n\n{totals}"
    prefix = f"{header}\n\n"
    suffix = f"\n\n{totals}"
    available = GROUP_CAPTION_LIMIT - len(prefix) - len(suffix)
    story_for_caption = story_text if len(story_text) <= available else story_text[: max(0, available - 1)] + "…"
    return f"{prefix}{story_for_caption}{suffix}"


async def finalize_dm_outing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = db.get_member(user.id)

    description = context.user_data.pop("dm_outing_desc", "Outing")
    teammates_selected = context.user_data.pop("teammates_selected", set())
    had_impact = context.user_data.pop("had_impact", False)
    story_text = context.user_data.pop("story_text", None)
    photo_file_id = context.user_data.pop("story_photo", None)
    context.user_data.clear()

    if not member:
        await context.bot.send_message(
            chat_id=user.id,
            text="Hmm, I couldn't find your membership record. Please ask an admin to add you.",
        )
        return

    team = member["team"]
    team_disp = db.team_display_name(team)
    name = member["display_name"]

    outing_id = db.create_outing(description)
    db.log_points(user.id, team, OUTING_POINTS, "outing_participation", outing_id)
    if had_impact:
        db.log_points(user.id, team, IMPACT_POINTS, "spiritual_impact", outing_id)
    if story_text is not None:
        db.log_points(user.id, team, SHARE_POINTS, "shared_reflection", outing_id)

    teammate_members = [m for m in db.get_all_members() if m["user_id"] in teammates_selected]
    for teammate in teammate_members:
        db.log_points(teammate["user_id"], teammate["team"], OUTING_POINTS, "outing_participation", outing_id)
    if teammate_members:
        company_bonus = OUTING_POINTS * len(teammate_members)
        db.log_points(user.id, team, company_bonus, "outing_company_bonus", outing_id)
    teammate_names = [
        m["display_name"] if m["team"] == team else f"{m['display_name']} ({db.team_display_name(m['team'])})"
        for m in teammate_members
    ]

    header, points_earned = build_dm_outing_header(
        name, team_disp, description, had_impact, story_text is not None, teammate_names
    )
    full_text = build_group_message(header, story_text)

    group_chat_id = db.get_setting("group_chat_id")
    if group_chat_id:
        chat_id = int(group_chat_id)
        if photo_file_id:
            caption = build_group_caption(header, story_text)
            await context.bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=caption)
        else:
            await context.bot.send_message(chat_id=chat_id, text=full_text)

    await context.bot.send_message(
        chat_id=user.id,
        text=f"✅ Logged! You earned {points_earned} pts for {team_disp}.",
    )


# ---------------------------------------------------------------------------
# Impact logging via DM: a member reports an impact made without a full
# outing (e.g. loving/praying for someone) — a flat-points share, plus an
# optional photo. No teammates can be credited on an impact.
# ---------------------------------------------------------------------------

async def logimpact_dm_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = db.get_member(user.id)
    if not member:
        await update.message.reply_text(
            "You're not registered as a member yet — ask a group admin to add you with "
            "/addmember first, then try again."
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text("Share more about the impact you made ❤️‍🔥")
    return IMPACT_AWAIT_STORY


async def impact_story_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.text == START_BUTTON_TEXT:
        return await restart_button_handler(update, context)
    if message.photo:
        await message.reply_text(
            "Let's get the story in words first — send it as a text message, and I'll ask "
            "for a photo separately right after."
        )
        return IMPACT_AWAIT_STORY

    context.user_data["impact_story"] = message.text
    await message.reply_text(
        "LETS GOO!! Would you like to send in a photo too?",
        reply_markup=build_yes_no_keyboard("impphoto"),
    )
    return IMPACT_ASK_PHOTO


async def impact_photo_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "impphoto:no":
        await query.edit_message_text("Processing...")
        await finalize_impact(update, context)
        return ConversationHandler.END

    await query.edit_message_text("Feel free to send me the photo 😁")
    return IMPACT_AWAIT_PHOTO


async def impact_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    context.user_data["impact_photo"] = message.photo[-1].file_id
    await message.reply_text("Got it! Logging your impact now...")
    await finalize_impact(update, context)
    return ConversationHandler.END


def build_impact_header(name: str, team_disp: str) -> str:
    lines = [
        f"❤️ {name} made an impact and earned +{IMPACT_LOG_POINTS} pts for {team_disp}!",
        f"(Total earned: {IMPACT_LOG_POINTS} pts)",
    ]
    return "\n".join(lines)


async def finalize_impact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = db.get_member(user.id)

    story_text = context.user_data.pop("impact_story", "")
    photo_file_id = context.user_data.pop("impact_photo", None)
    context.user_data.clear()

    if not member:
        await context.bot.send_message(
            chat_id=user.id,
            text="Hmm, I couldn't find your membership record. Please ask an admin to add you.",
        )
        return

    team = member["team"]
    team_disp = db.team_display_name(team)
    name = member["display_name"]

    outing_id = db.create_outing("❤️ Impact")
    db.log_points(user.id, team, IMPACT_LOG_POINTS, "impact_made", outing_id)

    header = build_impact_header(name, team_disp)
    full_text = build_group_message(header, story_text)

    group_chat_id = db.get_setting("group_chat_id")
    if group_chat_id:
        chat_id = int(group_chat_id)
        if photo_file_id:
            caption = build_group_caption(header, story_text)
            await context.bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=caption)
        else:
            await context.bot.send_message(chat_id=chat_id, text=full_text)

    await context.bot.send_message(
        chat_id=user.id,
        text=f"✅ Logged! You earned {IMPACT_LOG_POINTS} pts for {team_disp}.",
    )


# ---------------------------------------------------------------------------
# "Took new grounds" via DM: a member reports new grounds taken for a flat
# NEW_GROUND_POINTS reward — description, then a fuller story, then an
# optional photo (mirrors the self-report outing flow, minus the
# teammates/impact steps, which don't apply here).
# ---------------------------------------------------------------------------

async def newground_menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    member = db.get_member(user.id)
    if not member:
        await query.edit_message_text(
            "You're not registered as a member yet — ask a group admin to add you with "
            "/addmember first, then try again."
        )
        return ConversationHandler.END

    context.user_data.clear()
    await query.edit_message_text("Let's goooo 🔥 What grounds did you take?")
    return NG_AWAIT_DESCRIPTION


async def ng_description_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.text == START_BUTTON_TEXT:
        return await restart_button_handler(update, context)
    if message.photo:
        await message.reply_text(
            "Let's get it in words first — send it as a text message, and I'll ask for more "
            "detail (and a photo) separately right after."
        )
        return NG_AWAIT_DESCRIPTION

    context.user_data["ng_description"] = message.text.strip() or "New grounds"
    await message.reply_text("Now tell us more about it — how did it go?")
    return NG_AWAIT_STORY


async def ng_story_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.text == START_BUTTON_TEXT:
        return await restart_button_handler(update, context)
    if message.photo:
        await message.reply_text(
            "Let's get the story in words first — send it as a text message, and I'll ask "
            "for a photo separately right after."
        )
        return NG_AWAIT_STORY

    context.user_data["ng_story"] = message.text
    await message.reply_text(
        "Got it! Want to attach a photo too?",
        reply_markup=build_yes_no_keyboard("ngphoto"),
    )
    return NG_ASK_PHOTO


async def ng_photo_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "ngphoto:no":
        await query.edit_message_text("Processing...")
        await finalize_new_ground(update, context)
        return ConversationHandler.END

    await query.edit_message_text("Feel free to send me the photo 😁")
    return NG_AWAIT_PHOTO


async def ng_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    context.user_data["ng_photo"] = message.photo[-1].file_id
    await message.reply_text("Got it! Logging it now...")
    await finalize_new_ground(update, context)
    return ConversationHandler.END


def build_new_ground_header(name: str, team_disp: str, description: str) -> str:
    lines = [
        f"🔥 {name} took new grounds: \"{description}\" and earned +{NEW_GROUND_POINTS} pts for {team_disp}!",
        f"(Total earned: {NEW_GROUND_POINTS} pts)",
    ]
    return "\n".join(lines)


async def finalize_new_ground(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = db.get_member(user.id)

    description = context.user_data.pop("ng_description", "New grounds")
    story_text = context.user_data.pop("ng_story", "")
    photo_file_id = context.user_data.pop("ng_photo", None)
    context.user_data.clear()

    if not member:
        await context.bot.send_message(
            chat_id=user.id,
            text="Hmm, I couldn't find your membership record. Please ask an admin to add you.",
        )
        return

    team = member["team"]
    team_disp = db.team_display_name(team)
    name = member["display_name"]

    outing_id = db.create_outing(f"🔥 New grounds: {description}")
    db.log_points(user.id, team, NEW_GROUND_POINTS, "new_ground", outing_id)

    header = build_new_ground_header(name, team_disp, description)
    full_text = build_group_message(header, story_text)

    group_chat_id = db.get_setting("group_chat_id")
    if group_chat_id:
        chat_id = int(group_chat_id)
        if photo_file_id:
            caption = build_group_caption(header, story_text)
            await context.bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=caption)
        else:
            await context.bot.send_message(chat_id=chat_id, text=full_text)

    await context.bot.send_message(
        chat_id=user.id,
        text=f"✅ Logged! You earned {NEW_GROUND_POINTS} pts for {team_disp}.",
    )


# ---------------------------------------------------------------------------
# Private DM handler: capture reflections from users with a pending share
# ---------------------------------------------------------------------------

async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    outing_id = db.get_pending_share(user.id)
    if outing_id is None:
        await update.message.reply_text(
            "Thanks for the message! I don't currently have anything pending from you to share."
        )
        return

    member = db.get_member(user.id)
    if not member:
        db.pop_pending_share(user.id)
        return

    text = update.message.text
    db.pop_pending_share(user.id)
    db.log_points(user.id, member["team"], SHARE_POINTS, "shared_reflection", outing_id)

    group_chat_id = db.get_setting("group_chat_id")
    name = member["display_name"]

    await update.message.reply_text(
        f"Thank you for sharing! I've posted it to the group and added +{SHARE_POINTS} bonus points. 🙏"
    )

    if group_chat_id:
        await context.bot.send_message(
            chat_id=int(group_chat_id),
            text=f"💬 {name} shared about their outing experience:\n\n\"{text}\"\n\n(+{SHARE_POINTS} bonus pts!)",
        )


# ---------------------------------------------------------------------------
# Weekly Sunday summary
# ---------------------------------------------------------------------------

async def weekly_summary_job(context: ContextTypes.DEFAULT_TYPE):
    group_chat_id = db.get_setting("group_chat_id")
    if not group_chat_id:
        return
    text = "🗓️ *Weekly Points Update*\n\n" + build_summary_text()
    await context.bot.send_message(chat_id=int(group_chat_id), text=text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in your .env file (see .env.example).")

    db.init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # Basic commands
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("setgroup", setgroup_cmd))
    application.add_handler(CommandHandler("addmember", addmember_cmd))
    application.add_handler(CommandHandler("removemember", removemember_cmd))
    application.add_handler(CommandHandler("listmembers", listmembers_cmd))
    application.add_handler(CommandHandler("points", points_cmd))
    application.add_handler(CommandHandler("cleardata", cleardata_cmd))
    application.add_handler(CallbackQueryHandler(cleardata_confirm, pattern=r"^cleardata:"))

    # Outing logging conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("logouting", logouting_start, filters=filters.ChatType.GROUPS)],
        states={
            SELECT_ATTENDEES: [CallbackQueryHandler(attendee_toggle, pattern=r"^att:")],
            SELECT_IMPACT: [CallbackQueryHandler(impact_toggle, pattern=r"^imp:")],
        },
        fallbacks=[CommandHandler("cancel", logouting_cancel)],
    )
    application.add_handler(conv)

    # Outing logging via DM (members self-report their own outing), entered
    # either via /logouting <description> or by tapping the "Log an outing"
    # button from the /start menu.
    # Persistent "Start" reply-keyboard button: works as both an entry point
    # (resume the menu when idle) and a fallback (interrupt & reset mid-flow).
    restart_handler = MessageHandler(
        filters.ChatType.PRIVATE & filters.Regex(rf"^{re.escape(START_BUTTON_TEXT)}$"),
        restart_button_handler,
    )

    dm_conv = ConversationHandler(
        entry_points=[
            CommandHandler("logouting", logouting_dm_start, filters=filters.ChatType.PRIVATE),
            CommandHandler("logimpact", logimpact_dm_start, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(logouting_menu_start, pattern=r"^menu:log$"),
            CallbackQueryHandler(newground_menu_start, pattern=r"^menu:newground$"),
            restart_handler,
        ],
        states={
            LOG_TYPE_CHOICE: [CallbackQueryHandler(log_type_choice, pattern=r"^logtype:")],
            DM_ASK_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dm_description_message)
            ],
            DM_SELECT_TEAMMATES: [CallbackQueryHandler(dm_team_toggle, pattern=r"^dmteam:")],
            DM_ASK_IMPACT: [CallbackQueryHandler(dm_impact_answer, pattern=r"^dmimp:")],
            DM_ASK_SHARE: [CallbackQueryHandler(dm_share_answer, pattern=r"^dmshare:")],
            DM_AWAIT_STORY: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), dm_story_message)
            ],
            DM_ASK_PHOTO: [CallbackQueryHandler(dm_photo_answer, pattern=r"^dmphoto:")],
            DM_AWAIT_PHOTO: [MessageHandler(filters.PHOTO, dm_photo_message)],
            IMPACT_AWAIT_STORY: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), impact_story_message)
            ],
            IMPACT_ASK_PHOTO: [CallbackQueryHandler(impact_photo_answer, pattern=r"^impphoto:")],
            IMPACT_AWAIT_PHOTO: [MessageHandler(filters.PHOTO, impact_photo_message)],
            NG_AWAIT_DESCRIPTION: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), ng_description_message)
            ],
            NG_AWAIT_STORY: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), ng_story_message)
            ],
            NG_ASK_PHOTO: [CallbackQueryHandler(ng_photo_answer, pattern=r"^ngphoto:")],
            NG_AWAIT_PHOTO: [MessageHandler(filters.PHOTO, ng_photo_message)],
        },
        fallbacks=[CommandHandler("cancel", logouting_cancel), restart_handler],
    )
    application.add_handler(dm_conv)

    # Main-menu "View points & outings" button (outside the logging conversation)
    application.add_handler(CallbackQueryHandler(menu_view_cmd, pattern=r"^menu:view$"))

    # Passive member-tracking on every group message (low priority group)
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), track_group_member),
        group=1,
    )

    # Private DM handler for reflections
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND), private_message_handler)
    )

    # Weekly Sunday summary job
    tz = ZoneInfo(TIMEZONE)
    application.job_queue.run_daily(
        weekly_summary_job,
        time=dtime(hour=SUMMARY_HOUR, minute=0, tzinfo=tz),
        days=(6,),  # 0=Mon ... 6=Sun
        name="weekly_summary",
    )

    # Exclude edited-message updates: our handlers assume update.message is a
    # fresh message and don't handle the case where only update.edited_message
    # is set (e.g. a user edits their /logouting command right after sending it).
    allowed_updates = [
        u for u in Update.ALL_TYPES if u not in ("edited_message", "edited_channel_post")
    ]

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=allowed_updates)


if __name__ == "__main__":
    main()
