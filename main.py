import discord
import random
import json
import os
import logging
from decimal import Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# ── Config ──────────────────────────────────────────────────────────────────
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
PREFIX = "!"
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")
EURO_TICKET_COST = 10
ADMIN_USERNAME = "Katilho"
LOG_FILE = os.path.join(DATA_DIR, "discord.log")
LOG_MAX_BYTES = 32 * 1024 * 1024
LOG_BACKUP_COUNT = 5

if not TOKEN:
    raise RuntimeError("Missing Discord bot token. Set DISCORD_TOKEN or BOT_TOKEN.")

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
client = discord.Client(intents=intents)


def setup_logging() -> logging.Logger:
    formatter = logging.Formatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}",
        "%Y-%m-%d %H:%M:%S",
        style="{",
    )

    handler = RotatingFileHandler(
        filename=LOG_FILE,
        encoding="utf-8",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    handler.setFormatter(formatter)

    discord_logger = logging.getLogger("discord")
    discord_logger.setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.INFO)
    discord_logger.handlers.clear()
    discord_logger.addHandler(handler)

    app_logger = logging.getLogger("qubot")
    app_logger.setLevel(logging.INFO)
    app_logger.handlers.clear()
    app_logger.addHandler(handler)

    return app_logger


logger = setup_logging()


# ── Helpers: persistent "database" ──────────────────────────────────────────
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}


def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def get_user(db, user_id: str, user_name: str):
    """Return the persisted stats dictionary for a user."""
    if user_id not in db:
        db[user_id] = {
            "name": user_name,
            "coins": 1000,
            "bj_wins": 0,
            "bj_losses": 0,
            "bj_draws": 0,
            "begs": 0,
            "last_beg_time": None,
        }
    else:
        db[user_id]["name"] = user_name
    return db[user_id]


def is_admin_user(member: discord.abc.User) -> bool:
    return (
        member.name.casefold() == ADMIN_USERNAME.casefold()
        or getattr(member, "display_name", "").casefold() == ADMIN_USERNAME.casefold()
    )


def find_user_record_by_name(db: dict, user_name: str) -> tuple[str, dict] | None:
    lookup = user_name.casefold()
    for stored_user_id, stored_user in db.items():
        if stored_user.get("name", "").casefold() == lookup:
            return stored_user_id, stored_user
    return None


def format_blackjack_stats(user_id: str, user_data: dict) -> str:
    wins = user_data["bj_wins"]
    losses = user_data["bj_losses"]
    draws = user_data["bj_draws"]
    begs = user_data["begs"]
    decisive_games = wins + losses
    winrate = round(wins / decisive_games * 100, 2) if decisive_games else 0
    return (
        f"BlackJack stats from <@{user_id}>:\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Draws: {draws}\n"
        f"Winrate: {winrate}%\n"
        f"Begs: {begs}"
    )


def parse_amount(value: str) -> int | None:
    normalized = value.strip().lower()
    multiplier = 1

    if normalized.endswith("k"):
        multiplier = 1000
        normalized = normalized[:-1]

    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None

    if amount != amount.to_integral_value() and multiplier == 1:
        return None

    parsed = int(amount * multiplier)
    return parsed if parsed > 0 else None


def parse_blackjack_bet(args: list[str], balance: int) -> int | None:
    if balance <= 0:
        return None
    if len(args) <= 1:
        return 1
    if len(args) != 2:
        return None

    bet_arg = args[1].lower()
    if bet_arg == "half":
        return max(1, balance // 2)
    if bet_arg == "all":
        return balance

    return parse_amount(args[1])


# ── Card helpers ─────────────────────────────────────────────────────────────
SUITS = ["Ouros", "Espadas", "Paus", "Copas"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "Valete", "Dama", "Rei", "Ás"]


def card_value(rank: str) -> int:
    if rank in ("Valete", "Dama", "Rei"):
        return 10
    if rank == "Ás":
        return 11  # Ace starts as 11; bust-reduction handled in hand_value
    return int(rank)


def hand_value(hand: list[tuple]) -> int:
    total = sum(card_value(r) for r, _ in hand)
    aces = sum(1 for r, _ in hand if r == "Ás")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def fmt_hand(hand: list[tuple]) -> str:
    return ", ".join(f"{r} de {s}" for r, s in hand)


def new_deck() -> list[tuple]:
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


# Active blackjack games: { channel_id: { user_id: { deck, player, dealer, bet } } }
bj_games: dict = {}

BJ_HIT_EMOJI = "☝️"
BJ_STAND_EMOJI = "✋"
BJ_DOUBLE_EMOJI = "✌️"
BJ_CONTROLS = (BJ_HIT_EMOJI, BJ_STAND_EMOJI, BJ_DOUBLE_EMOJI)
BJ_THUMBNAIL_PATH = "images/blackjack.png"
BJ_THUMBNAIL_NAME = "blackjack.png"
BJ_RESULT_IMAGE_PATH = "images/you-win.png"
BJ_RESULT_IMAGE_NAME = "you-win.png"


def bj_controls_text() -> str:
    return (
        f"{BJ_HIT_EMOJI} Pedir carta  •  "
        f"{BJ_STAND_EMOJI} Parar  •  "
        f"{BJ_DOUBLE_EMOJI} Dobrar"
    )


def bj_result_colour(result: str | None) -> int:
    if result == "win":
        return 0x2ECC71
    if result == "loss":
        return 0xE74C3C
    if result == "draw":
        return 0xF1C40F
    return 0x2ECC71


def make_bj_thumbnail_file() -> discord.File:
    return discord.File(BJ_THUMBNAIL_PATH, filename=BJ_THUMBNAIL_NAME)


def make_bj_result_file(result: str | None) -> discord.File | None:
    if result != "win":
        return None
    return discord.File(BJ_RESULT_IMAGE_PATH, filename=BJ_RESULT_IMAGE_NAME)


def clear_bj_game(channel_id: int, user_id: int):
    channel_games = bj_games.get(channel_id)
    if not channel_games:
        return
    channel_games.pop(user_id, None)
    if not channel_games:
        bj_games.pop(channel_id, None)


def build_bj_embed(
    game: dict,
    balance: int,
    note: str,
    reveal_dealer: bool = False,
    result: str | None = None,
):
    player = game["player"]
    dealer = game["dealer"]
    player_value = hand_value(player)

    embed = discord.Embed(title="Jogo de BlackJack", colour=bj_result_colour(result))
    embed.set_author(
        name=f"{game['player_name']}'s game",
        icon_url=game["player_avatar_url"],
    )
    embed.set_thumbnail(url=f"attachment://{BJ_THUMBNAIL_NAME}")

    if reveal_dealer:
        dealer_value = hand_value(dealer)
        embed.add_field(
            name=f"Dealer's cards → {dealer_value}",
            value=fmt_hand(dealer),
            inline=False,
        )
    else:
        embed.add_field(
            name="Dealer's cards",
            value=f"{dealer[0][0]} de {dealer[0][1]}, *Carta escondida*",
            inline=False,
        )

    embed.add_field(
        name=f"{game['player_name']}'s cards → {player_value}",
        value=fmt_hand(player),
        inline=False,
    )

    if result == "win":
        embed.set_image(url=f"attachment://{BJ_RESULT_IMAGE_NAME}")

    if result is None:
        footer_text = f"{note} | 🪙 Tens {balance} coins e apostaste {game['bet']}."
    else:
        footer_text = note
    embed.set_footer(text=footer_text)
    return embed


async def fetch_bj_message(channel, game: dict):
    try:
        return await channel.fetch_message(game["message_id"])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def update_bj_message(
    channel,
    game: dict,
    balance: int,
    note: str,
    reveal_dealer: bool = False,
    result: str | None = None,
):
    game_message = await fetch_bj_message(channel, game)
    if not game_message:
        return None
    thumbnail_file = make_bj_thumbnail_file()
    result_file = make_bj_result_file(result)
    attachments = [thumbnail_file]
    if result_file:
        attachments.append(result_file)
    await game_message.edit(
        embed=build_bj_embed(game, balance, note, reveal_dealer, result),
        attachments=attachments,
    )
    return game_message


async def clear_bj_reactions(channel, game: dict):
    game_message = await fetch_bj_message(channel, game)
    if not game_message:
        return
    try:
        await game_message.clear_reactions()
    except (discord.Forbidden, discord.HTTPException):
        pass


async def add_bj_controls(game_message: discord.Message):
    for emoji in BJ_CONTROLS:
        try:
            await game_message.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException):
            break


async def settle_blackjack(channel, channel_id: int, user_id: int):
    game = bj_games.get(channel_id, {}).get(user_id)
    if not game:
        return

    db = load_db()
    user = get_user(db, game["user_key"], game["player_name"])
    dealer = game["dealer"]
    deck = game["deck"]
    bet = game["bet"]
    player_value = hand_value(game["player"])

    if player_value > 21:
        user["bj_losses"] += 1
        note = f"Passaste dos 21. Ficaste com {user['coins']} coins."
        result = "loss"
    else:
        while hand_value(dealer) < 17:
            dealer.append(deck.pop())

        dealer_value = hand_value(dealer)
        if dealer_value > 21 or player_value > dealer_value:
            payout = bet * 2
            profit = bet
            user["coins"] += payout
            user["bj_wins"] += 1
            note = f"Ganhaste {profit} coins e ficaste com {user['coins']} coins. 😃"
            result = "win"
        elif player_value == dealer_value:
            user["coins"] += bet
            user["bj_draws"] += 1
            note = (
                f"Empate. A aposta voltou para ti e ficaste com {user['coins']} coins."
            )
            result = "draw"
        else:
            user["bj_losses"] += 1
            note = f"Perdeste a aposta e ficaste com {user['coins']} coins."
            result = "loss"

    save_db(db)
    await update_bj_message(
        channel,
        game,
        user["coins"],
        note,
        reveal_dealer=True,
        result=result,
    )
    await clear_bj_reactions(channel, game)
    clear_bj_game(channel_id, user_id)


async def blackjack_hit(channel, channel_id: int, user_id: int):
    game = bj_games.get(channel_id, {}).get(user_id)
    if not game:
        await channel.send("No active game. Start one with `!blackjack <bet>`.")
        return

    db = load_db()
    user = get_user(db, game["user_key"], game["player_name"])
    game["player"].append(game["deck"].pop())
    player_value = hand_value(game["player"])

    if player_value >= 21:
        save_db(db)
        await settle_blackjack(channel, channel_id, user_id)
        return

    await update_bj_message(
        channel,
        game,
        user["coins"],
        bj_controls_text(),
    )


async def blackjack_double(channel, channel_id: int, user_id: int):
    game = bj_games.get(channel_id, {}).get(user_id)
    if not game:
        await channel.send("No active game. Start one with `!blackjack <bet>`.")
        return

    db = load_db()
    user = get_user(db, game["user_key"], game["player_name"])
    if len(game["player"]) != 2:
        await channel.send("Só podes dobrar na primeira jogada.")
        return
    if user["coins"] < game["bet"]:
        await channel.send("Não tens coins suficientes para dobrar a aposta.")
        return

    user["coins"] -= game["bet"]
    game["bet"] *= 2
    save_db(db)
    game["player"].append(game["deck"].pop())
    await settle_blackjack(channel, channel_id, user_id)


# ── Euromilhões helpers ───────────────────────────────────────────────────────
def draw_euro():
    nums = sorted(random.sample(range(1, 51), 5))
    stars = sorted(random.sample(range(1, 13), 2))
    return nums, stars


def euro_prize(
    m_nums: int, m_stars: int, p_nums: list, p_stars: list, nums: list, stars: list
) -> int:
    if p_nums == nums and p_stars == stars:
        return 1_000_000_000
    if p_nums == nums:
        return 1_000_000
    if p_stars == stars:
        return 100_000
    return int(pow(5, m_nums + m_stars) * m_nums + 5 * m_stars)


def fmt_euro_line(numbers: list[int], stars: list[int]) -> str:
    number_text = "  ".join(str(number) for number in numbers)
    star_text = "  ".join(str(star) for star in stars)
    return f"Numbers: {number_text} - Stars: {star_text}"


# ── Events ────────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    logger.info("Logged in as %s", client.user)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if client.user is None or payload.user_id == client.user.id:
        return

    game = bj_games.get(payload.channel_id, {}).get(payload.user_id)
    if not game or game.get("message_id") != payload.message_id:
        return

    emoji = str(payload.emoji)
    if emoji not in BJ_CONTROLS:
        return

    channel = client.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(payload.channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    game_message = await fetch_bj_message(channel, game)
    if game_message:
        reacting_user = payload.member or client.get_user(payload.user_id)
        if reacting_user is None:
            try:
                reacting_user = await client.fetch_user(payload.user_id)
            except (discord.NotFound, discord.HTTPException):
                reacting_user = None
        if reacting_user is not None:
            try:
                await game_message.remove_reaction(payload.emoji, reacting_user)
            except (discord.Forbidden, discord.HTTPException):
                pass

    if emoji == BJ_HIT_EMOJI:
        await blackjack_hit(channel, payload.channel_id, payload.user_id)
    elif emoji == BJ_STAND_EMOJI:
        await settle_blackjack(channel, payload.channel_id, payload.user_id)
    elif emoji == BJ_DOUBLE_EMOJI:
        await blackjack_double(channel, payload.channel_id, payload.user_id)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    db = load_db()
    uid = message.author.id
    user_key = str(uid)
    user_name = message.author.display_name
    user = get_user(db, user_key, user_name)
    content = message.content.strip()
    msg = content.lower()
    args = content.split()
    command = args[0].lower() if args else ""

    # ── !help ────────────────────────────────────────────────────────────────
    if msg in ("!help",):
        embed = discord.Embed(
            title="QuBot", description="Commands of the bot", colour=0x5865F2
        )
        embed.add_field(
            name="!blackjack or !bj <bet>",
            value="Starts a game of blackjack. `!bj` bets 1, `!bj half` bets half, and `!bj all` bets everything.",
            inline=False,
        )
        embed.add_field(
            name="!euromilhoes or !euro",
            value="Starts a game of euromilhoes.",
            inline=False,
        )
        embed.add_field(
            name="!coinflip",
            value="Flip a coin.",
            inline=False,
        )
        embed.add_field(
            name="!mystats",
            value="Shows the history of win/loss of your blackjack games.",
            inline=False,
        )
        embed.add_field(
            name="!stats",
            value="Shows blackjack stats for a user or the total Dealer vs Players stats.",
            inline=False,
        )
        embed.add_field(
            name="!money",
            value="Tells you how much money you have.",
            inline=False,
        )
        embed.add_field(
            name="!rank",
            value="Shows a ranking of sorted by richest.",
            inline=False,
        )
        embed.add_field(
            name="!give",
            value="Gives an amount of your money to someone you desire.",
            inline=False,
        )
        embed.add_field(
            name="!beg",
            value="Lose your shame and beg for money.",
            inline=False,
        )
        await message.channel.send(embed=embed)

    # ── !money ───────────────────────────────────────────────────────────────
    elif msg == "!money":
        await message.channel.send(
            f"💰 **{message.author.mention}** has **{user['coins']}** coins."
        )

    # ── !mystats ─────────────────────────────────────────────────────────────
    elif msg == "!mystats":
        await message.channel.send(format_blackjack_stats(user_key, user))

    # ── !stats ───────────────────────────────────────────────────────────────
    elif command == "!stats":
        if len(args) == 2:
            target_record = find_user_record_by_name(db, args[1])
            if target_record is None:
                await message.channel.send(
                    f"User `{args[1]}` was not found in the database."
                )
                return
            target_user_id, target_user = target_record
            await message.channel.send(
                format_blackjack_stats(target_user_id, target_user)
            )
        else:
            dealer_wins = sum(data.get("bj_losses", 0) for data in db.values())
            player_wins = sum(data.get("bj_wins", 0) for data in db.values())
            await message.channel.send(
                f"The Dealer {dealer_wins} vs {player_wins} The Players"
            )

    # ── !rank ─────────────────────────────────────────────────────────────────
    elif msg == "!rank":
        ranking = sorted(db.values(), key=lambda data: data["coins"], reverse=True)[:10]
        lines = [
            f"{i + 1}. {data['name']} — {data['coins']} coins"
            for i, data in enumerate(ranking)
        ]
        await message.channel.send("🏆 **Richest players:**\n" + "\n".join(lines))

    # ── !setmoney ────────────────────────────────────────────────────────────
    elif command == "!setmoney":
        if not is_admin_user(message.author):
            await message.channel.send("You are not allowed to use this command.")
            return
        if len(args) != 3:
            await message.channel.send("Usage: `!setmoney <user_name> <amount>`")
            return

        target_name = args[1]
        target_record = find_user_record_by_name(db, target_name)
        if target_record is None:
            await message.channel.send(
                f"User `{target_name}` was not found in the database."
            )
            return

        try:
            amount = int(args[2])
        except ValueError:
            await message.channel.send("Money amount must be a number.")
            return

        if amount < 0:
            await message.channel.send("Money amount must be zero or positive.")
            return

        _, target_user = target_record
        target_user["coins"] = amount
        save_db(db)
        await message.channel.send(
            f"✅ Set **{target_user['name']}** to **{amount}** coins."
        )

    # ── !give ─────────────────────────────────────────────────────────────────
    elif msg.startswith("!give"):
        if len(message.mentions) == 1 and len(args) == 3:
            try:
                amt = int(args[2])
                target = str(message.mentions[0].id)
                t_data = get_user(db, target, message.mentions[0].display_name)
                if amt <= 0:
                    await message.channel.send("Amount must be positive.")
                elif user["coins"] < amt:
                    await message.channel.send("You don't have enough coins.")
                else:
                    user["coins"] -= amt
                    t_data["coins"] += amt
                    save_db(db)
                    await message.channel.send(
                        f"✅ **{message.author.mention}** gave **{amt}** coins to **{message.mentions[0].mention}**."
                    )
            except ValueError:
                await message.channel.send("Usage: `!give @user <amount>`")
        else:
            await message.channel.send("Usage: `!give @user <amount>`")

    # ── !beg ──────────────────────────────────────────────────────────────────
    elif msg == "!beg":
        import time

        if user["coins"] > 0:
            await message.channel.send(
                f"🚫 **{message.author.mention}** you can only beg when you're broke! You have {user['coins']} coins."
            )
            return

        current_time = time.time()
        beg_cooldown = 3600
        max_begs_before_shame = 5

        if user.get("last_beg_time"):
            time_since_last_beg = current_time - user["last_beg_time"]
            if time_since_last_beg < beg_cooldown:
                remaining = int(beg_cooldown - time_since_last_beg)
                minutes = remaining // 60
                seconds = remaining % 60
                await message.channel.send(
                    f"🚫 **{message.author.mention}** begging has a cooldown! Wait {minutes}m {seconds}s before begging again."
                )
                return

        user["last_beg_time"] = current_time
        earned = random.randint(1, 100)
        user["coins"] += earned
        user["begs"] += 1

        shame_messages = [
            f"🙏 **{message.author.mention}** pediu e recebeu **{earned}** moedas. Total de pedidos: {user['begs']}. Já pensaste em fazer um Patreon?",
            f"🙏 **{message.author.mention}** pediu **{earned}** moedas ({user['begs']} vezes). O teu futuro está a sofrer.",
            f"🙏 **{message.author.mention}** recebeu **{earned}** moedas a pedir. Com {user['begs']} pedidos, estás a fazer speedrun da pobreza.",
            f"🙏 **{message.author.mention}** conseguiu **{earned}** moedas (tentativa #{user['begs']}). Até o algoritmo sente vergonha alheia.",
            f"🙏 **{message.author.mention}** pediu de novo por **{earned}** moedas. Até o Benfica tem mais estabilidade financeira.",
            f"🙏 **{message.author.mention}** pediu de novo por **{earned}** moedas. O teu histórico bancário é agora uma telenovela.",
            f"🙏 **{message.author.mention}** conseguiu **{earned}** moedas. Com {user['begs']} pedidos, alcançaste o estatuto de whale... mas ao contrário.",
        ]

        if user["begs"] >= max_begs_before_shame:
            shame_messages.extend(
                [
                    f"🚨 **{message.author.mention}** pediu {user['begs']} vezes. O teu LinkedIn devia dizer 'Entusiasta de Criptomoedas.'",
                    f"⚠️ **{message.author.mention}** ({user['begs']} pedidos) está a fazer speedrun do fundo do poço. Empenho impressionante.",
                    f"📢 **{message.author.mention}** é agora um Pedidor Nível {user['begs']}. Próximo desbloqueio: respeitar-te a ti mesmo.",
                    f"🔔 **{message.author.mention}** ({user['begs']} pedidos) transcendeu a pobreza numa marca pessoal.",
                    f"💀 **{message.author.mention}** pediu {user['begs']} vezes. Neste ponto, não é raiva—é documentar a tua história de origem.",
                ]
            )

        save_db(db)
        await message.channel.send(random.choice(shame_messages))

    # ── !coinflip / !cf ───────────────────────────────────────────────────────
    elif msg.startswith(("!coinflip", "!cf")):
        if len(args) != 3:
            await message.channel.send("Usage: `!coinflip <heads/tails> <bet>`")
        else:
            choice = args[1].lower()
            if choice not in ("heads", "tails"):
                await message.channel.send("Choose **heads** or **tails**.")
            else:
                try:
                    bet = int(args[2])
                    if bet <= 0 or user["coins"] < bet:
                        await message.channel.send("Invalid bet amount.")
                    else:
                        result = random.choice(["heads", "tails"])
                        if result == choice:
                            user["coins"] += bet
                            save_db(db)
                            await message.channel.send(
                                f"🪙 It's **{result}**! You won **{bet}** coins! You now have **{user['coins']}**."
                            )
                        else:
                            user["coins"] -= bet
                            save_db(db)
                            await message.channel.send(
                                f"🪙 It's **{result}**! You lost **{bet}** coins. You now have **{user['coins']}**."
                            )
                except ValueError:
                    await message.channel.send("Bet must be a number.")

    # ── !blackjack / !bj ─────────────────────────────────────────────────────
    elif command in ("!blackjack", "!bj"):
        cid = message.channel.id
        if cid in bj_games and uid in bj_games[cid]:
            await message.channel.send(
                "You already have an active game! Use `!hit`, `!stand`, or the reactions on your game post."
            )
            return
        bet = parse_blackjack_bet(args, user["coins"])
        if bet is None:
            await message.channel.send(
                "Usage: `!blackjack [bet]`, `!blackjack half`, or `!blackjack all`\n`!bj` bets 1 coin by default."
            )
            return
        if bet <= 0 or user["coins"] < bet:
            await message.channel.send("Invalid bet amount.")
            return

        deck = new_deck()
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        bj_games.setdefault(cid, {})[uid] = {
            "deck": deck,
            "player": player,
            "dealer": dealer,
            "bet": bet,
            "message_id": None,
            "player_name": message.author.display_name,
            "player_avatar_url": message.author.display_avatar.url,
            "user_key": user_key,
        }
        user["coins"] -= bet
        save_db(db)

        pv = hand_value(player)
        game = bj_games[cid][uid]
        game_message = await message.channel.send(
            embed=build_bj_embed(
                game,
                user["coins"],
                bj_controls_text(),
            )
        )
        game["message_id"] = game_message.id

        if pv == 21:
            payout = int(bet * 2.5)
            profit = payout - bet
            user["coins"] += payout
            user["bj_wins"] += 1
            save_db(db)
            await update_bj_message(
                message.channel,
                game,
                user["coins"],
                f"Blackjack natural. Ganhaste {profit} coins e ficaste com {user['coins']} coins. 😃",
                reveal_dealer=True,
                result="win",
            )
            clear_bj_game(cid, uid)
            return

        await add_bj_controls(game_message)

    # ── !hit ──────────────────────────────────────────────────────────────────
    elif msg == "!hit":
        await blackjack_hit(message.channel, message.channel.id, uid)

    # ── !stand ────────────────────────────────────────────────────────────────
    elif msg == "!stand":
        if not bj_games.get(message.channel.id, {}).get(uid):
            await message.channel.send("No active game.")
            return
        await settle_blackjack(message.channel, message.channel.id, uid)

    # ── !euromilhoes / !euro ──────────────────────────────────────────────────
    elif msg.startswith(("!euromilhoes", "!euro")):
        # Usage: !euro n1 n2 n3 n4 n5 s1 s2
        if len(args) != 8:
            await message.channel.send(
                f"Usage: `!euro <5 numbers 1-50> <2 stars 1-12>`\nExample: `!euro 1 7 23 34 49 3 9`\nTicket cost: {EURO_TICKET_COST} coins."
            )
            return
        if user["coins"] < EURO_TICKET_COST:
            await message.channel.send(
                f"You need at least {EURO_TICKET_COST} coins to play Euromilhoes."
            )
            return
        try:
            p_nums = sorted([int(args[i]) for i in range(1, 6)])
            p_stars = sorted([int(args[i]) for i in range(6, 8)])
        except ValueError:
            await message.channel.send("All values must be integers.")
            return
        if not all(1 <= n <= 50 for n in p_nums):
            await message.channel.send("Numbers must be between 1 and 50.")
            return
        if not all(1 <= s <= 12 for s in p_stars):
            await message.channel.send("Stars must be between 1 and 12.")
            return
        if len(set(p_nums)) != 5 or len(set(p_stars)) != 2:
            await message.channel.send("No repeated numbers or stars allowed.")
            return

        user["coins"] -= EURO_TICKET_COST
        nums, stars = draw_euro()
        m_nums = len(set(p_nums) & set(nums))
        m_stars = len(set(p_stars) & set(stars))
        prize = euro_prize(m_nums, m_stars, p_nums, p_stars, nums, stars)
        user["coins"] += prize
        save_db(db)
        await message.channel.send(
            f"🎟️  Your numbers:\n"
            f"{fmt_euro_line(p_nums, p_stars)}\n"
            f"🏆  Winning key:\n"
            f"{fmt_euro_line(nums, stars)}\n"
            f"You got {m_nums} matching numbers and {m_stars} matching stars.\n"
            f"You paid {EURO_TICKET_COST} to play and won {prize}! 😃"
        )


client.run(TOKEN, log_handler=None)
