import asyncio
import logging
from datetime import datetime
from typing import Optional

import discord
import genshin
from dateutil.relativedelta import relativedelta
from discord.ext import tasks, commands
from genshin.models import DailyReward
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from common.db import session
from common.genshin_server import ServerEnum
from common.logging import logger
from datamodels.genshin_user import GenshinUser
from datamodels.scheduling import ScheduledItem, ItemType


class HoyolabDailyCheckin(commands.Cog):
    DATABASE_KEY = ItemType.DAILY_CHECKIN
    CHECKIN_TIMEZONE = ServerEnum.ASIA
    TASK_INTERVAL_HOURS = 4

    def __init__(self, bot: discord.Bot = None):
        self.bot = bot
        self.start_up = False

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.start_up:
            self.start_up = True

            # To better align with check in time, we schedule the task such that it runs every
            # {TASK_INTERVAL_HOURS} hours and one run will coincide with the checkin reset time.
            next_checkin_time = self.CHECKIN_TIMEZONE.day_beginning + relativedelta(
                day=1
            )
            time_until = (
                next_checkin_time - self.CHECKIN_TIMEZONE.current_time
            ).total_seconds()
            await_seconds = time_until % (self.TASK_INTERVAL_HOURS * 3600)
            logger.info(f"Next daily checkin scan is in {await_seconds} seconds")
            await asyncio.sleep(await_seconds)

            self.job.start()

    @tasks.loop(hours=4)
    async def job(self):
        logger.info(f"Daily checkin scan begins")
        for discord_id in session.execute(
            select(GenshinUser.discord_id.distinct())
        ).scalars():
            discord_user = await self.bot.fetch_user(discord_id)
            channel = await discord_user.create_dm()
            try:
                await self.checkin(discord_id, channel)
            except Exception:
                logging.exception(f"Cannot check in for {discord_id}")

    async def checkin(self, discord_id: int, channel: discord.DMChannel):
        embeds = []
        failure_embeds = []

        for account in session.execute(
            select(GenshinUser).where(GenshinUser.discord_id == discord_id)
        ).scalars():
            account: GenshinUser

            if not account.hoyolab_token:
                continue

            gs: genshin.GenshinClient = account.client

            # Validate cookies
            try:
                await gs.get_reward_info()
            except genshin.errors.InvalidCookies:
                account.hoyolab_token = None
                session.merge(account)
                session.commit()
                failure_embeds.append(discord.Embed(
                    title=":warning: Account Access Failure",
                    description=f"ltoken has expired for Hoyolab ID {account.mihoyo_id}.\n"
                                f"This may be because you have changed your password recently.\n"
                                f"Please register again if you want to continue using the bot."
                ))
                continue

            task: ScheduledItem = session.get(
                ScheduledItem, (account.mihoyo_id, self.DATABASE_KEY)
            )

            if (
                not task
                or task.scheduled_at
                < self.CHECKIN_TIMEZONE.day_beginning.replace(tzinfo=None)
            ):
                try:
                    reward = await self.claim_reward(gs)
                except Exception:
                    logger.exception("Cannot claim daily rewards")
                    continue

                if reward is not None:
                    embed = discord.Embed()
                    embeds.append(embed)
                    embed.description = (
                        f"Claimed daily reward - **{reward.amount} {reward.name}** "
                        f"| Hoyolab ID {account.mihoyo_id}"
                    )

                    try:
                        for uid in account.genshin_uids:
                            notes = await gs.get_notes(uid)
                            resin_capped = notes.current_resin == notes.max_resin
                            exp_completed_at = max(
                                exp.completed_at for exp in notes.expeditions
                            )
                            embed.add_field(
                                name=f"<:resin:926812413238595594> {notes.current_resin}/{notes.max_resin}",
                                value=":warning: capped OMG"
                                if resin_capped
                                else f"capped <t:{int(notes.resin_recovered_at.timestamp())}:R>",
                            )
                            embed.add_field(
                                name=f"{len(notes.expeditions)}/{notes.max_expeditions} expeditions dispatched",
                                value=":warning: all done"
                                if exp_completed_at <= datetime.now().astimezone()
                                else f"done <t:{int(exp_completed_at.timestamp())}:R>",
                            )
                            embed.description += f"\nUID-`{uid}`"
                    except Exception:
                        logger.exception("Cannot get resin data")

                session.merge(
                    ScheduledItem(
                        id=account.mihoyo_id,
                        type=self.DATABASE_KEY,
                        scheduled_at=self.CHECKIN_TIMEZONE.day_beginning,
                        done=True,
                    )
                )
                session.commit()

            await gs.close()

        if embeds:
            await channel.send(
                "I've gone ahead and checked in for you. Have a nice day!",
                embeds=embeds,
            )

        if failure_embeds:
            await channel.send(
                embeds=failure_embeds,
            )

    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=4, max=600)
    )
    async def claim_reward(
        self, client: genshin.GenshinClient
    ) -> Optional[DailyReward]:
        try:
            return await client.claim_daily_reward(reward=True)
        except genshin.errors.AlreadyClaimed:
            logger.exception(
                f"Daily reward is already claimed for {client.cookies.get('ltuid')}"
            )
            return None
        except Exception:
            logger.exception(
                f"Cannot claim daily rewards for {client.cookies.get('ltuid')}"
            )
            raise
