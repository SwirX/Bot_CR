import random

import discord
from discord.ext import commands

import config

EIGHTBALL_ANSWERS = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes — definitely.",
    "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
    "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Don't count on it.",
    "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful.",
]

JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs. 🐛",
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my robot a joke... it went over its head. 🤖",
    "Why was the robot angry? Because someone kept pushing its buttons.",
    "There are only 10 types of people: those who understand binary, and those who don't.",
    "Why did the developer go broke? Because he used up all his cache. 💸",
    "A SQL query walks into a bar, goes up to two tables and asks: 'Can I JOIN you?'",
    "Why do we tell actors to 'break a leg'? Because every play has a cast. 🎭",
]

FACTS = [
    "Honey never spoils — archaeologists have found 3,000-year-old honey in Egyptian tombs.",
    "Octopuses have three hearts and blue blood.",
    "A day on Venus is longer than a year on Venus.",
    "The Eiffel Tower grows about 15 cm taller in summer due to thermal expansion.",
    "Bananas are berries, but strawberries are not.",
    "Your brain uses about 20% of your body's total energy.",
    "There are more possible chess games than atoms in the observable universe. ♟️",
    "Lightning strikes the Earth about 100 times every second. ⚡",
]

COMPLIMENTS = [
    "You have an amazing sense of humour!", "Your ideas are genuinely brilliant.",
    "You make this server a better place. 💛", "Your code is cleaner than a sorted array.",
    "You light up every conversation you join.", "You're the kind of person others look up to.",
    "Your vibes are immaculate. ✨",
]

SLAPS = [
    "{actor} slapped {target} with a servo motor! ⚙️",
    "{actor} slapped {target} with a fish. 🐟",
    "{actor} slapped {target} with a rolled-up user manual.",
    "{actor} slapped {target} so hard their Discord lagged.",
    "{actor} gently slapped {target} with a keyboard. ⌨️",
]

HUGS = [
    "{actor} gave {target} a warm hug. 🤗",
    "{actor} hugged {target} like they just won a hackathon. 🏆",
    "{actor} wrapped {target} in a group-hug energy. 💞",
    "{actor} gave {target} a robot-arm hug. 🦾",
]

ROASTS = [
    "You're like a 404 error — not found in my list of concerns.",
    "You bring everyone joy... when you leave the room. 😄",
    "Your brain is the size of a microcontroller with no program loaded.",
    "You're the reason they put instructions on shampoo bottles.",
    "I'd roast you, but my circuits can't handle that much cringe.",
    "You're like a robot without batteries — full of potential, zero output.",
    "You're the human equivalent of a segfault. 💥",
]

QUOTES = [
    "The best way to predict the future is to invent it. — Alan Kay",
    "First, solve the problem. Then, write the code. — John Johnson",
    "Innovation distinguishes between a leader and a follower. — Steve Jobs",
    "Programs must be written for people to read. — Harold Abelson",
    "Simplicity is the soul of efficiency. — Austin Freeman",
    "The most disastrous thing that you can ever learn is your first programming language. — Alan Kay",
    "Feedback is a gift. Even when it's wrapped in an exception. 🎁",
]


class Fun(commands.Cog):
    """Lighthearted commands to keep the server lively."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="8ball", description="Ask the magic 8-ball a question.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def eight_ball(self, ctx, *, question: str):
        await ctx.send(f"❓ {ctx.author.display_name} asked: *{question}*\n🎱 {random.choice(EIGHTBALL_ANSWERS)}")

    @commands.hybrid_command(name="coinflip", description="Flip a coin.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def coinflip(self, ctx):
        await ctx.send(f"🪙 The coin lands on: **{random.choice(['Heads', 'Tails'])}**!")

    @commands.hybrid_command(name="dice", description="Roll a die (default 6 sides).")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def dice(self, ctx, sides: int = 6):
        sides = max(2, min(sides, 1000))
        await ctx.send(f"🎲 You rolled a **{random.randint(1, sides)}** (d{sides})!")

    @commands.hybrid_command(name="slap", description="Slap someone.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def slap(self, ctx, member: discord.Member):
        if member == self.bot.user:
            await ctx.send("Nice try, but I don't feel a thing. 🤖💢")
            return
        template = random.choice(SLAPS)
        await ctx.send(template.format(actor=ctx.author.display_name, target=member.display_name))

    @commands.hybrid_command(name="hug", description="Hug someone.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def hug(self, ctx, member: discord.Member):
        template = random.choice(HUGS)
        await ctx.send(template.format(actor=ctx.author.display_name, target=member.display_name))

    @commands.hybrid_command(name="joke", description="Tell a random tech joke.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def joke(self, ctx):
        await ctx.send(random.choice(JOKES))

    @commands.hybrid_command(name="fact", description="Share a random fun fact.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def fact(self, ctx):
        await ctx.send(f"💡 Fun fact: {random.choice(FACTS)}")

    @commands.hybrid_command(name="compliment", description="Get a compliment.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def compliment(self, ctx):
        await ctx.send(f"💛 {ctx.author.mention}, {random.choice(COMPLIMENTS)}")

    # ── club website ───────────────────────────────────────────
    @commands.hybrid_command(name="website", description="The Robotics Club's website.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def website(self, ctx):
        embed = discord.Embed(
            title="🌐 Robotics Club",
            description="Projects, teams, events and how to join — all on our site.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="🔗 Website", value=config.WEBSITE_URL, inline=False)
        embed.set_footer(text="Come check out what we're building!")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Visit robotics.ma", url=config.WEBSITE_URL,
            style=discord.ButtonStyle.link,
        ))
        await ctx.send(embed=embed, view=view)

    # ── moar fun ───────────────────────────────────────────────
    @commands.hybrid_command(name="rps", description="Play rock-paper-scissors against the bot.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def rps(self, ctx, choice: str):
        choice = choice.strip().lower()
        emoji = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        if choice not in emoji:
            await ctx.send("⚠️ Pick `rock`, `paper` or `scissors`.")
            return
        bot_choice = random.choice(list(emoji))
        beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
        outcome = "It's a tie! 🤝"
        if beats[choice] == bot_choice:
            outcome = "You win! 🎉"
        elif beats[bot_choice] == choice:
            outcome = "I win! 🤖"
        await ctx.send(f"{emoji[choice]} vs {emoji[bot_choice]} — {outcome}")

    @commands.hybrid_command(name="ship", description="Ship two members with a compatibility score.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ship(self, ctx, first: discord.Member, second: discord.Member):
        score = random.randint(1, 100)
        heart = "❤️" * max(1, score // 20)
        mood = "💔" if score < 40 else ("💕" if score < 75 else "💘")
        await ctx.send(
            f"{mood} **{first.display_name}** x **{second.display_name}** — "
            f"{score}% compatible {heart}"
        )

    @commands.hybrid_command(name="choose", description="The bot picks one of your options.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def choose(self, ctx, options: str):
        choices = [option for option in options.replace(",", " ").split() if option.strip()]
        if not choices:
            await ctx.send("⚠️ Give me some options: `!choose pizza sushi tacos`")
            return
        await ctx.send(f"🧠 I choose: **{random.choice(choices)}**")

    @commands.hybrid_command(name="reverse", description="Reverse your text.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def reverse(self, ctx, *, text: str):
        await ctx.send(f"↩️ {text[::-1]}")

    @commands.hybrid_command(name="clap", description="👏 Emphasis 👏 on 👏 every 👏 word.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def clap(self, ctx, *, text: str):
        words = text.split()
        if not words:
            await ctx.send("⚠️ Give me a phrase to clapify.")
            return
        await ctx.send("👏 " + " 👏 ".join(words) + " 👏")

    @commands.hybrid_command(name="roast", description="Roast someone (playfully).")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def roast(self, ctx, member: discord.Member):
        if member == self.bot.user:
            await ctx.send("I'm immune to roasts. 🤖🔥")
            return
        await ctx.send(f"{member.mention}, {random.choice(ROASTS)}")

    @commands.hybrid_command(name="quote", description="A random tech/robotics quote.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def quote(self, ctx):
        await ctx.send(f"💬 {random.choice(QUOTES)}")


async def setup(bot):
    await bot.add_cog(Fun(bot))