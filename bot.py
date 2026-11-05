import os
import re
import secrets
import string
import hashlib
import sqlite3
import datetime
from datetime import timedelta
import asyncio
from dotenv import load_dotenv
import os



import discord
from discord import app_commands
from discord.ext import commands, tasks
load_dotenv()
TOKEN = os.getenv("bot_token")
HEADS_ROLE_ID = 1567495476033495171
STAFF_ROLE_ID = 1567494359698374726
LOA_ROLE_ID = 1567503753643303005

STAFF_REVIEWS_CHANNEL_ID = 1567493351454933084
ANNOUNCEMENT_CHANNEL_ID = 1567492713375473755
BREAK_REQUEST_CHANNEL_ID = 1567493657597055038

MINIMUM_SHIFT_HOURS = 2.5
DATABASE_FILE = "staff_database.db"

db = sqlite3.connect(DATABASE_FILE)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")

db.executescript("""
CREATE TABLE IF NOT EXISTS licenses (
    user_id INTEGER PRIMARY KEY,
    license TEXT UNIQUE NOT NULL,
    passcode_hash TEXT NOT NULL,
    positive INTEGER NOT NULL DEFAULT 0,
    negative INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance (
    user_id INTEGER PRIMARY KEY,
    sign_in TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    point_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    sign_in TEXT NOT NULL,
    sign_out TEXT NOT NULL,
    hours REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    duration TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer_id INTEGER
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_id INTEGER,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""")
db.commit()

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)



def calculate_attendance_percentage(user_id, days=7):
    """Calculate sign-in percentage over the last N days"""
    since = (utcnow() - timedelta(days=days)).isoformat()
    total_days = days
    
    signed_in_days = db.execute("""
        SELECT DISTINCT DATE(sign_in) as day 
        FROM attendance 
        WHERE user_id = ? AND sign_in >= ?
    """, (user_id, since)).fetchall()
    
    unique_days = len(signed_in_days)
    percentage = round((unique_days / total_days) * 100, 1)
    return percentage

def get_previous_week_points(user_id):
    """Get total points from the previous week for comparison"""
    end = utcnow() - timedelta(days=7)
    start = end - timedelta(days=7)
    
    rows = db.execute("""
        SELECT point_type, SUM(amount) as total
        FROM point_history
        WHERE user_id = ? AND created_at >= ? AND created_at <= ?
        GROUP BY point_type
    """, (user_id, start.isoformat(), end.isoformat())).fetchall()
    
    prev_pos = 0
    prev_neg = 0
    for row in rows:
        if row['point_type'] == 'positive':
            prev_pos = row['total']
        elif row['point_type'] == 'negative':
            prev_neg = row['total']
    return prev_pos, prev_neg

def calculate_change_percentage(current, previous):
    if previous == 0:
        return "+∞%" if current > 0 else "0%"
    change = ((current - previous) / previous) * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}%"

async def send_weekly_report():
    """Generate and send the weekly report"""
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        return
    
    licenses = db.execute("SELECT * FROM licenses WHERE active=1").fetchall()
    
    embed = discord.Embed(
        title="📊 Weekly Staff Report",
        description=f"Week ending {utcnow().strftime('%A, %d %B %Y')}",
        color=discord.Color.purple(),
        timestamp=utcnow()
    )
    
    for record in licenses:
        user = await bot.fetch_user(record['user_id'])
        if not user:
            continue
        
        attendance_pct = calculate_attendance_percentage(record['user_id'])
        prev_pos, prev_neg = get_previous_week_points(record['user_id'])
        
        pos_change = calculate_change_percentage(record['positive'], prev_pos)
        neg_change = calculate_change_percentage(record['negative'], prev_neg)
        
        embed.add_field(
            name=f"📋 {user.display_name} — {record['license']}",
            value=(
                f"**Attendance:** {attendance_pct}%\n"
                f"**Positive Points:** {record['positive']}  ({pos_change})\n"
                f"**Negative Points:** {record['negative']}  ({neg_change})\n"
                f"**Net Score:** {record['positive'] - record['negative']}"
            ),
            inline=False
        )
    
    await channel.send(embed=embed)

@tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=datetime.timezone.utc))
async def weekly_report_task():
    """Runs every Friday at 9:00 AM UTC"""
    if utcnow().weekday() == 4:  # 4 = Friday
        await send_weekly_report()

@weekly_report_task.before_loop
async def before_weekly_report():
    await bot.wait_until_ready()


@bot.tree.command(name="generatereport", description="Generate weekly report now (Heads only)")
async def generatereport(interaction: discord.Interaction):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await send_weekly_report()
    await interaction.followup.send("✅ Weekly report generated!", ephemeral=True)
    await audit(interaction.user.id, "Manual report generated", details="Weekly report run via command")

def iso_now():
    return utcnow().isoformat()


def has_role(member, role_id):
    return any(role.id == role_id for role in getattr(member, "roles", []))


def is_head(member):
    return has_role(member, HEADS_ROLE_ID)


def is_staff(member):
    return is_head(member) or has_role(member, STAFF_ROLE_ID)


def get_license(user_id):
    return db.execute(
        "SELECT * FROM licenses WHERE user_id=? AND active=1",
        (user_id,)
    ).fetchone()


def hash_passcode(passcode):
    return hashlib.sha256(passcode.encode()).hexdigest()


def generate_license():
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(chars) for _ in range(6))
        if not db.execute(
            "SELECT 1 FROM licenses WHERE license=?",
            (code,)
        ).fetchone():
            return code


def generate_passcode():
    return f"{secrets.randbelow(10000):04d}"


async def send_log(title, description, color=discord.Color.blurple()):
    channel = bot.get_channel(STAFF_REVIEWS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STAFF_REVIEWS_CHANNEL_ID)
        except discord.HTTPException:
            return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=utcnow()
    )
    await channel.send(embed=embed)


async def audit(actor_id, action, target_id=None, details=""):
    db.execute(
        """
        INSERT INTO audit_logs (actor_id, action, target_id, details, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (actor_id, action, target_id, details, iso_now())
    )
    db.commit()

    await send_log(
        "Staff Audit Log",
        f"**Action:** {action}\n"
        f"**Actor:** <@{actor_id}>\n"
        f"**Target:** {f'<@{target_id}>' if target_id else 'N/A'}\n"
        f"**Details:** {details}"
    )


async def dm_user(user, message=None, embed=None):
    try:
        await user.send(content=message, embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def parse_duration(value):
    match = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", value.lower())
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    if amount <= 0:
        return None

    if unit == "m":
        return datetime.timedelta(minutes=amount)
    if unit == "h":
        return datetime.timedelta(hours=amount)
    if unit == "d":
        return datetime.timedelta(days=amount)

    return None


def format_duration(delta):
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")

    return " ".join(parts) or "less than a minute"


async def get_member(guild, user_id):
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
    return member


class BreakDecisionView(discord.ui.View):
    def __init__(self, request_id):
        super().__init__(timeout=None)
        self.request_id = request_id

        accept_button = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.green,
            custom_id=f"break_accept:{request_id}"
        )
        reject_button = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.red,
            custom_id=f"break_reject:{request_id}"
        )

        accept_button.callback = self.accept
        reject_button.callback = self.reject

        self.add_item(accept_button)
        self.add_item(reject_button)

    async def resolve(self, interaction, approved):
        if not is_head(interaction.user):
            await interaction.response.send_message(
                "Only Heads can review break requests.",
                ephemeral=True
            )
            return

        request = db.execute(
            "SELECT * FROM breaks WHERE id=?",
            (self.request_id,)
        ).fetchone()

        if not request or request["status"] != "pending":
            await interaction.response.send_message(
                "This request has already been reviewed.",
                ephemeral=True
            )
            return

        member = await get_member(interaction.guild, request["user_id"])
        if member is None:
            await interaction.response.send_message(
                "The staff member could not be found.",
                ephemeral=True
            )
            return

        status = "approved" if approved else "rejected"

        if approved:
            loa_role = interaction.guild.get_role(LOA_ROLE_ID)
            if loa_role is None:
                await interaction.response.send_message(
                    "LOA role was not found. Configure LOA_ROLE_ID.",
                    ephemeral=True
                )
                return

            try:
                await member.add_roles(
                    loa_role,
                    reason=f"Break request #{self.request_id} approved"
                )
            except discord.HTTPException:
                await interaction.response.send_message(
                    "I could not assign the LOA role. Check my role permissions.",
                    ephemeral=True
                )
                return

            expires_at = (
                utcnow() + parse_duration(request["duration"])
            ).isoformat()

            db.execute(
                """
                UPDATE breaks
                SET status='approved', reviewer_id=?, expires_at=?
                WHERE id=?
                """,
                (interaction.user.id, expires_at, self.request_id)
            )
            db.commit()

            await dm_user(
                member,
                f"Your break request was approved. Your LOA ends in {request['duration']}."
            )

        else:
            db.execute(
                "UPDATE breaks SET status='rejected', reviewer_id=? WHERE id=?",
                (interaction.user.id, self.request_id)
            )
            db.commit()
            await dm_user(member, "Your break request was rejected.")

        await audit(
            interaction.user.id,
            f"Break request {status}",
            member.id,
            f"Request #{self.request_id}"
        )

        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green() if approved else discord.Color.red()
        embed.add_field(
            name="Decision",
            value=f"{status.title()} by {interaction.user.mention}",
            inline=False
        )

        await interaction.response.edit_message(embed=embed, view=self)

    async def accept(self, interaction):
        await self.resolve(interaction, True)

    async def reject(self, interaction):
        await self.resolve(interaction, False)


class AttendanceReviewView(discord.ui.View):
    def __init__(self, review_id):
        super().__init__(timeout=None)
        self.review_id = review_id

        approve_button = discord.ui.Button(
            label="Approve Negative Point",
            style=discord.ButtonStyle.danger,
            custom_id=f"attendance_approve:{review_id}"
        )
        reject_button = discord.ui.Button(
            label="Reject Penalty",
            style=discord.ButtonStyle.secondary,
            custom_id=f"attendance_reject:{review_id}"
        )

        approve_button.callback = self.approve
        reject_button.callback = self.reject

        self.add_item(approve_button)
        self.add_item(reject_button)

    async def approve(self, interaction):
        if not is_head(interaction.user):
            await interaction.response.send_message(
                "Only Heads can approve attendance penalties.",
                ephemeral=True
            )
            return

        review = db.execute(
            "SELECT * FROM attendance_reviews WHERE id=?",
            (self.review_id,)
        ).fetchone()

        if not review or review["status"] != "pending":
            await interaction.response.send_message(
                "This review has already been handled.",
                ephemeral=True
            )
            return

        if not get_license(review["user_id"]):
            await interaction.response.send_message(
                "This staff member has no active license.",
                ephemeral=True
            )
            return

        db.execute(
            "UPDATE licenses SET negative=negative+1 WHERE user_id=?",
            (review["user_id"],)
        )

        db.execute(
            """
            INSERT INTO point_history
            (user_id, moderator_id, point_type, amount, reason, created_at)
            VALUES (?, ?, 'negative', 1, ?, ?)
            """,
            (
                review["user_id"],
                interaction.user.id,
                f"Early sign-out review #{self.review_id}",
                iso_now()
            )
        )

        db.execute(
            """
            UPDATE attendance_reviews
            SET status='approved', reviewer_id=?
            WHERE id=?
            """,
            (interaction.user.id, self.review_id)
        )
        db.commit()

        member = await get_member(interaction.guild, review["user_id"])
        if member:
            await dm_user(
                member,
                "A Head reviewed your early sign-out and approved one negative point."
            )

        await audit(
            interaction.user.id,
            "Attendance penalty approved",
            review["user_id"],
            f"Review #{self.review_id}"
        )

        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(
            name="Decision",
            value=f"Penalty approved by {interaction.user.mention}",
            inline=False
        )

        await interaction.response.edit_message(embed=embed, view=self)

    async def reject(self, interaction):
        if not is_head(interaction.user):
            await interaction.response.send_message(
                "Only Heads can review attendance penalties.",
                ephemeral=True
            )
            return

        review = db.execute(
            "SELECT * FROM attendance_reviews WHERE id=?",
            (self.review_id,)
        ).fetchone()

        if not review or review["status"] != "pending":
            await interaction.response.send_message(
                "This review has already been handled.",
                ephemeral=True
            )
            return

        db.execute(
            """
            UPDATE attendance_reviews
            SET status='rejected', reviewer_id=?
            WHERE id=?
            """,
            (interaction.user.id, self.review_id)
        )
        db.commit()

        member = await get_member(interaction.guild, review["user_id"])
        if member:
            await dm_user(
                member,
                "A Head reviewed your early sign-out. No negative point was applied."
            )

        await audit(
            interaction.user.id,
            "Attendance penalty rejected",
            review["user_id"],
            f"Review #{self.review_id}"
        )

        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(
            name="Decision",
            value=f"Penalty rejected by {interaction.user.mention}",
            inline=False
        )

        await interaction.response.edit_message(embed=embed, view=self)


class AnnouncementModal(discord.ui.Modal):
    def __init__(self, announcement_type):
        super().__init__(title=f"{announcement_type.title()} Announcement")
        self.announcement_type = announcement_type

        if announcement_type == "resignation":
            self.license_input = discord.ui.TextInput(
                label="Staff License",
                max_length=6
            )
            self.add_item(self.license_input)

        self.message_input = discord.ui.TextInput(
            label="Announcement",
            style=discord.TextStyle.paragraph,
            max_length=2000
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction):
        channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if channel is None:
            await interaction.response.send_message(
                "Announcement channel not found.",
                ephemeral=True
            )
            return

        message = self.message_input.value

        if self.announcement_type == "resignation":
            license_code = self.license_input.value.upper()
            record = db.execute(
                "SELECT * FROM licenses WHERE license=? AND active=1",
                (license_code,)
            ).fetchone()

            if not record:
                await interaction.response.send_message(
                    "Invalid active staff license.",
                    ephemeral=True
                )
                return

            member = await get_member(interaction.guild, record["user_id"])
            if member is None:
                await interaction.response.send_message(
                    "Staff member not found.",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="Staff Resignation",
                description=message,
                color=discord.Color.red()
            )
            await channel.send(content=member.mention, embed=embed)

        else:
            embed = discord.Embed(
                title="Staff News",
                description=message,
                color=discord.Color.blue()
            )
            await channel.send(embed=embed)

        await audit(
            interaction.user.id,
            f"Announcement: {self.announcement_type}",
            details=message[:500]
        )

        await interaction.response.send_message(
            "Announcement sent.",
            ephemeral=True
        )


class BreakModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Staff Break Request")

        self.reason = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            max_length=1000
        )

        self.duration = discord.ui.TextInput(
            label="Duration (examples: 30m, 2h, 3d)",
            max_length=20
        )

        self.add_item(self.reason)
        self.add_item(self.duration)

    async def on_submit(self, interaction):
        delta = parse_duration(self.duration.value)

        if delta is None:
            await interaction.response.send_message(
                "Use a duration such as 30m, 2h, or 3d.",
                ephemeral=True
            )
            return

        existing = db.execute(
            """
            SELECT id FROM breaks
            WHERE user_id=? AND status='approved' AND expires_at>?
            """,
            (interaction.user.id, iso_now())
        ).fetchone()

        pending = db.execute(
            """
            SELECT id FROM breaks
            WHERE user_id=? AND status='pending'
            """,
            (interaction.user.id,)
        ).fetchone()

        if existing or pending:
            await interaction.response.send_message(
                "You already have an active or pending break request.",
                ephemeral=True
            )
            return

        cursor = db.execute(
            """
            INSERT INTO breaks (user_id, reason, duration, requested_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                interaction.user.id,
                self.reason.value,
                self.duration.value,
                iso_now()
            )
        )
        db.commit()

        request_id = cursor.lastrowid

        embed = discord.Embed(
            title="Staff Break Request",
            color=discord.Color.orange(),
            timestamp=utcnow()
        )
        embed.add_field(name="Staff", value=interaction.user.mention)
        embed.add_field(name="Reason", value=self.reason.value)
        embed.add_field(name="Duration", value=self.duration.value)
        embed.add_field(name="Request ID", value=str(request_id))

        channel = bot.get_channel(BREAK_REQUEST_CHANNEL_ID)
        if channel is None:
            db.execute(
                "UPDATE breaks SET status='cancelled' WHERE id=?",
                (request_id,)
            )
            db.commit()
            await interaction.response.send_message(
                "Break request channel not found.",
                ephemeral=True
            )
            return

        await channel.send(
            embed=embed,
            view=BreakDecisionView(request_id)
        )


        await audit(interaction.user.id,"Break requested",interaction.user.id,f"Request #{request_id}: {self.duration}")   
        await interaction.response.send_message("Your break request has been submitted.",ephemeral=True)


@tasks.loop(minutes=1)
async def expire_breaks():
    now = iso_now()

    rows = db.execute(
        """
        SELECT * FROM breaks
        WHERE status='approved' AND expires_at IS NOT NULL AND expires_at<=?
        """,
        (now,)
    ).fetchall()

    for row in rows:
        guilds = list(bot.guilds)

        for guild in guilds:
            member = await get_member(guild, row["user_id"])
            role = guild.get_role(LOA_ROLE_ID)

            if member and role and role in member.roles:
                try:
                    await member.remove_roles(
                        role,
                        reason=f"Break request #{row['id']} expired"
                    )
                except discord.HTTPException:
                    continue

        db.execute(
            "UPDATE breaks SET status='completed' WHERE id=?",
            (row["id"],)
        )
        db.commit()

        member = bot.get_user(row["user_id"])
        if member:
            await dm_user(member, "Your approved break has ended. Your LOA role has been removed.")

        await send_log(
            "Break Expired",
            f"Break request #{row['id']} has expired for <@{row['user_id']}>."
        )


@expire_breaks.before_loop
async def before_expire_breaks():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    for row in db.execute(
        "SELECT id FROM breaks WHERE status='pending'"
    ).fetchall():
        bot.add_view(BreakDecisionView(row["id"]))

    for row in db.execute(
        "SELECT id FROM attendance_reviews WHERE status='pending'"
    ).fetchall():
        bot.add_view(AttendanceReviewView(row["id"]))

    if not expire_breaks.is_running():
        expire_breaks.start()

    await bot.tree.sync()
    print(f"Logged in as {bot.user}")


@bot.tree.command(name="assignlicense", description="Assign a staff license")
@app_commands.describe(member="Staff member")
async def assignlicense(interaction: discord.Interaction, member: discord.Member):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return

    if not is_staff(member):
        await interaction.response.send_message("That member is not staff.", ephemeral=True)
        return

    if get_license(member.id):
        await interaction.response.send_message("This staff member already has a license.", ephemeral=True)
        return

    code = generate_license()
    passcode = generate_passcode()

    db.execute(
        """
        INSERT INTO licenses (user_id, license, passcode_hash, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (member.id, code, hash_passcode(passcode), iso_now())
    )
    db.commit()

    embed = discord.Embed(title="Staff License Assigned", color=discord.Color.green())
    embed.add_field(name="Staff Member", value=member.mention)
    embed.add_field(name="License", value=code)
    embed.add_field(name="Staff Passcode", value=passcode)
    embed.add_field(name="Positive Points", value="0")
    embed.add_field(name="Negative Points", value="0")

    sent = await dm_user(member, embed=embed)

    await interaction.response.send_message(
        f"License created: `{code}`. Passcode delivery: {'DM sent' if sent else 'DM failed; securely deliver the passcode manually.'}",
        ephemeral=True
    )

    await audit(interaction.user.id, "License assigned", member.id, code)


@bot.tree.command(name="revokelicense", description="Revoke a staff license")
@app_commands.describe(member="Staff member")
async def revokelicense(interaction: discord.Interaction, member: discord.Member):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return

    record = get_license(member.id)
    if not record:
        await interaction.response.send_message("No active license found.", ephemeral=True)
        return

    db.execute(
        "UPDATE licenses SET active=0 WHERE user_id=?",
        (member.id,)
    )
    db.commit()

    await audit(interaction.user.id, "License revoked", member.id, record["license"])

    await interaction.response.send_message(
        f"License `{record['license']}` revoked for {member.mention}.",
        ephemeral=True
    )


@bot.tree.command(name="verifylicense", description="Verify your staff license and passcode")
@app_commands.describe(license_code="Your 6-character license", passcode="Your 4-digit passcode")
async def verifylicense(
    interaction: discord.Interaction,
    license_code: str,
    passcode: str
):
    record = db.execute(
        "SELECT * FROM licenses WHERE license=? AND user_id=? AND active=1",
        (license_code.upper(), interaction.user.id)
    ).fetchone()

    if not record or not secrets.compare_digest(
        record["passcode_hash"],
        hash_passcode(passcode)
    ):
        await interaction.response.send_message(
            "License or passcode is incorrect.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Your staff license and passcode are valid.",
        ephemeral=True
    )
@bot.tree.command(name="mylicense", description="View your license and points")
async def mylicense(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return

    record = get_license(interaction.user.id)
    if not record:
        await interaction.response.send_message("No active license found.", ephemeral=True)
        return

    embed = discord.Embed(title="Your Staff License", color=discord.Color.blue())
    embed.add_field(name="License", value=record["license"])
    embed.add_field(name="Positive Points", value=record["positive"])
    embed.add_field(name="Negative Points", value=record["negative"])
    embed.add_field(name="Net Points", value=record["positive"] - record["negative"])

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stafflicense", description="View a staff member's license")
@app_commands.describe(member="Staff member")
async def stafflicense(interaction: discord.Interaction, member: discord.Member):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return

    record = get_license(member.id)
    if not record:
        await interaction.response.send_message("No active license found.", ephemeral=True)
        return

    embed = discord.Embed(title="Staff License Record", color=discord.Color.blue())
    embed.add_field(name="Staff", value=member.mention)
    embed.add_field(name="License", value=record["license"])
    embed.add_field(name="Positive", value=record["positive"])
    embed.add_field(name="Negative", value=record["negative"])
    embed.add_field(name="Net", value=record["positive"] - record["negative"])

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="givepoints", description="Give positive or negative staff points")
@app_commands.describe(
    member="Staff member",
    point_type="Positive or negative",
    amount="Number of points",
    reason="Reason"
)
@app_commands.choices(point_type=[
    app_commands.Choice(name="Positive", value="positive"),
    app_commands.Choice(name="Negative", value="negative")
])
async def givepoints(
    interaction: discord.Interaction,
    member: discord.Member,
    point_type: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 100],
    reason: str
):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return

    record = get_license(member.id)
    if not record:
        await interaction.response.send_message("Staff member has no active license.", ephemeral=True)
        return

    column = point_type.value

    db.execute(
        f"UPDATE licenses SET {column}={column}+? WHERE user_id=?",
        (amount, member.id)
    )

    db.execute(
        """
        INSERT INTO point_history
        (user_id, moderator_id, point_type, amount, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (member.id, interaction.user.id, column, amount, reason, iso_now())
    )
    db.commit()

    updated = get_license(member.id)

    embed = discord.Embed(title="Staff Points Updated", color=discord.Color.green())
    embed.add_field(name="Staff", value=member.mention)
    embed.add_field(name="License", value=updated["license"])
    embed.add_field(name="Point Type", value=column.title())
    embed.add_field(name="Amount", value=str(amount))
    embed.add_field(name="Reason", value=reason)
    embed.add_field(name="Positive", value=updated["positive"])
    embed.add_field(name="Negative", value=updated["negative"])
    embed.add_field(name="Net", value=updated["positive"] - updated["negative"])

    await interaction.response.send_message(embed=embed)
    await dm_user(member, embed=embed)
    await audit(interaction.user.id, "Points awarded", member.id, f"{column} +{amount}: {reason}")
@bot.tree.command(name="pointhistory", description="View staff point history")
@app_commands.describe(member="Staff member")
async def pointhistory(interaction: discord.Interaction, member: discord.Member = None):
    if member is None:
        member = interaction.user

    if member.id != interaction.user.id and not is_head(interaction.user):
        await interaction.response.send_message("You can only view your own history.", ephemeral=True)
        return

    if not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return

    rows = db.execute(
        """
        SELECT * FROM point_history
        WHERE user_id=?
        ORDER BY id DESC LIMIT 10
        """,
        (member.id,)
    ).fetchall()

    if not rows:
        await interaction.response.send_message("No point history found.", ephemeral=True)
        return

    embed = discord.Embed(title=f"Point History: {member.display_name}")
    for row in rows:
        embed.add_field(
            name=f"{row['point_type'].title()} {row['amount']} — {row['created_at'][:10]}",
            value=f"Reason: {row['reason']}\nGiven by: <@{row['moderator_id']}>",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="matt", description="Sign in for your shift")
async def matt(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return

    if not get_license(interaction.user.id):
        await interaction.response.send_message("You need an active license first.", ephemeral=True)
        return

    existing = db.execute(
        "SELECT * FROM attendance WHERE user_id=?",
        (interaction.user.id,)
    ).fetchone()

    if existing:
        await interaction.response.send_message("You are already signed in.", ephemeral=True)
        return

    now = iso_now()
    db.execute(
        "INSERT INTO attendance (user_id, sign_in) VALUES (?, ?)",
        (interaction.user.id, now)
    )
    db.commit()

    await interaction.response.send_message(
        f"Signed in at {now}.",
        ephemeral=True
    )
    await audit(interaction.user.id, "Staff signed in", interaction.user.id, now)

@bot.tree.command(name="sendmsg", description="Send a direct message to a user")
@app_commands.describe(member="Who to message", message="What to send")
async def sendmsg(interaction: discord.Interaction, member: discord.Member, message: str):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return
    success = await dm_user(member, f"📩 **Message from Staff Team:**\n\n{message}")
    if success:
        await interaction.response.send_message(
            f"✅ Message sent to {member.mention}:\n> {message}",
            ephemeral=True
        )
        await audit(interaction.user.id, "DM sent", member.id, message[:200])
    else:
        await interaction.response.send_message(
            f"❌ Could not message {member.mention}. They may have DMs disabled or not share a server with us.",
            ephemeral=True
        )


@bot.tree.command(name="unmatt", description="Sign out from your shift")
async def unmatt(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    existing = db.execute(
        "SELECT * FROM attendance WHERE user_id=?",
        (interaction.user.id,)
    ).fetchone()
    if not existing:
        await interaction.response.send_message("You are not signed in.", ephemeral=True)
        return
    start = datetime.datetime.fromisoformat(existing["sign_in"])
    end = utcnow()
    hours = (end - start).total_seconds() / 3600
    db.execute("DELETE FROM attendance WHERE user_id=?", (interaction.user.id,))
    db.commit()
    if hours < MINIMUM_SHIFT_HOURS:
        cursor = db.execute(
            """
            INSERT INTO attendance_reviews
            (user_id, sign_in, sign_out, hours, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (interaction.user.id, existing["sign_in"], end.isoformat(), hours, iso_now())
        )
        db.commit()
        review_id = cursor.lastrowid
        embed = discord.Embed(
            title="Early Sign-Out Review",
            description=f"{interaction.user.mention} signed out before completing 2.5 hours.",
            color=discord.Color.orange()
        )
        embed.add_field(name="Time Worked", value=f"{hours:.2f} hours")
        embed.add_field(name="Sign In", value=existing["sign_in"])
        embed.add_field(name="Sign Out", value=end.isoformat())
        embed.add_field(name="Review ID", value=str(review_id))
        channel = bot.get_channel(STAFF_REVIEWS_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed, view=AttendanceReviewView(review_id))
        await dm_user(
            interaction.user,
            "You signed out before completing 2.5 hours. A Head will review whether a negative point applies."
        )
        await interaction.response.send_message(
            "Signed out. Your early sign-out has been sent for Head review.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"Signed out after {hours:.2f} hours.",
            ephemeral=True
        )
    await audit(interaction.user.id, "Staff signed out", interaction.user.id, f"{hours:.2f} hours")



@bot.tree.command(name="attendanceboard", description="View currently signed-in staff")
async def attendanceboard(interaction: discord.Interaction):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return

    rows = db.execute("SELECT * FROM attendance ORDER BY sign_in").fetchall()
    embed = discord.Embed(title="Attendance Dashboard", color=discord.Color.blue())

    if not rows:
        embed.description = "Nobody is currently signed in."
    else:
        for row in rows:
            start = datetime.datetime.fromisoformat(row["sign_in"])
            hours = (utcnow() - start).total_seconds() / 3600
            embed.add_field(
                name=f"Staff: <@{row['user_id']}>",
                value=f"Signed in: {row['sign_in']}\nHours: {hours:.2f}",
                inline=False
            )

    await interaction.response.send_message(embed=embed, ephemeral=True)
@bot.tree.command(name="ann", description="Send a staff announcement")
@app_commands.choices(announcement_type=[
    app_commands.Choice(name="News", value="news"),
    app_commands.Choice(name="Resignation", value="resignation")
])
async def ann(interaction: discord.Interaction, announcement_type: app_commands.Choice[str]):
    if not is_head(interaction.user):
        await interaction.response.send_message("Heads only.", ephemeral=True)
        return

    await interaction.response.send_modal(AnnouncementModal(announcement_type.value))


@bot.tree.command(name="break", description="Request a staff break")
async def staff_break(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return

    await interaction.response.send_modal(BreakModal())


@bot.event
async def on_ready():
    for row in db.execute("SELECT id FROM breaks WHERE status='pending'").fetchall():
        bot.add_view(BreakDecisionView(row["id"]))

    for row in db.execute("SELECT id FROM attendance_reviews WHERE status='pending'").fetchall():
        bot.add_view(AttendanceReviewView(row["id"]))

    if not expire_breaks.is_running():
        expire_breaks.start()
    if not weekly_report_task.is_running():
        weekly_report_task.start()
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")


@tasks.loop(minutes=1)
async def expire_breaks():
    now = iso_now()
    rows = db.execute(
        """
        SELECT * FROM breaks
        WHERE status='approved' AND expires_at IS NOT NULL AND expires_at<=?
        """,
        (now,)
    ).fetchall()

    for row in rows:
        removed = False

        for guild in bot.guilds:
            member = await get_member(guild, row["user_id"])
            role = guild.get_role(LOA_ROLE_ID)

            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason=f"Break #{row['id']} expired")
                    removed = True
                except discord.HTTPException:
                    pass

        db.execute(
            "UPDATE breaks SET status='completed' WHERE id=?",
            (row["id"],)
        )
        db.commit()

        user = bot.get_user(row["user_id"])
        if user:
            await dm_user(
                user,
                "Your approved break has ended. The bot has processed your LOA expiration."
            )

        await send_log(
            "Break Expired",
            f"Break #{row['id']} ended for <@{row['user_id']}>. LOA role removed: {removed}."
        )


@expire_breaks.before_loop
async def before_expire_breaks():
    await bot.wait_until_ready()

@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return
    
    # If message is a DM — forward it to staff reviews channel
    if isinstance(message.channel, discord.DMChannel):
        staff_channel = bot.get_channel(STAFF_REVIEWS_CHANNEL_ID)
        if staff_channel and not message.author.bot:
            embed = discord.Embed(
                title="📩 Reply from User",
                description=message.content or "*No text (attachment only)*",
                color=discord.Color.teal(),
                timestamp=utcnow()
            )
            embed.set_author(name=message.author.display_name, icon_url=message.author.avatar.url if message.author.avatar else None)
            
            # Add attachments if any
            if message.attachments:
                file_list = "\n".join(f"- {a.filename}" for a in message.attachments)
                embed.add_field(name="Attachments", value=file_list, inline=False)
            
            await staff_channel.send(embed=embed)
    
    await bot.process_commands(message)
if not TOKEN:
    raise RuntimeError("Set the DISCORD_BOT_TOKEN environment variable.")

bot.run(TOKEN)