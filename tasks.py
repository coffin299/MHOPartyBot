"""日曜 0:00（JST）の全削除と、停止中に過ぎた日曜の取りこぼし。"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from discord.ext import commands, tasks

from service import REASON_SUNDAY, guild_lock, purge_all_parties
from views import close_card, database_from, refresh_board

logger = logging.getLogger(__name__)

# 日曜の曜日番号は 6。
SUNDAY = 6

# 起動時の確認と、0:00 のタスクが重なっても削除は1回にする。
_purge_lock = asyncio.Lock()


def _japan_time() -> tzinfo:
    """日曜 0:00 に使う日本時間。データが無い Windows では UTC+9 を使う。"""
    try:
        # tzdata がある環境では、地域名のタイムゾーンを使う。
        return ZoneInfo("Asia/Tokyo")
    except ZoneInfoNotFoundError:
        # 日本に夏時間は無いので、固定の UTC+9 でも同じ時刻になる。
        return timezone(timedelta(hours=9))


# 日曜 0:00 の判定に使う日本時間。
JST = _japan_time()


def most_recent_sunday(now: datetime) -> date:
    """いま以前で、いちばん近い日曜の日付を返す。"""
    # 日曜なら 0 日、月曜なら 1 日、土曜なら 6 日戻る。
    days_back = (now.weekday() - SUNDAY) % 7
    return now.date() - timedelta(days=days_back)


async def purge_if_due(bot: commands.Bot) -> None:
    """記録より新しい日曜を過ぎていれば全削除し、カードと掲示板を空にする。"""
    now = datetime.now(JST)
    target = most_recent_sunday(now)
    async with _purge_lock:
        database = database_from(bot)
        last = database.get_last_purge()
        # 初回は消さず、今日以前の直近の日曜を処理済みにする。
        if last is None:
            database.set_last_purge(target)
            logger.info(
                "初回起動のため削除せず、基準日を記録しました: %s",
                target.isoformat(),
            )
            return
        # 同じ日曜を、再接続のたびに繰り返さない。
        if last >= target:
            return
        parties = purge_all_parties(database)
        # UI 更新の前に記録し、重なった呼び出しが二重削除しないようにする。
        database.set_last_purge(target)
        boards = database.list_boards()
        logger.info("日曜リセットで %s 件のパーティを削除しました", len(parties))
    guild_ids = {party.guild_id for party in parties}
    # 募集がゼロでも、空の掲示板へ戻す対象に含める。
    guild_ids.update(board.guild_id for board in boards)
    for guild_id in sorted(guild_ids):
        async with guild_lock(guild_id):
            for party in parties:
                # 他ギルドのカードは、このロックの中では触らない。
                if party.guild_id != guild_id:
                    continue
                await close_card(bot, party, REASON_SUNDAY)
            await refresh_board(bot, guild_id)


def start_sunday_task(bot: commands.Bot) -> None:
    """毎日 0:00 JST に起動し、日曜だけ全削除する。"""

    @tasks.loop(time=time(0, 0, tzinfo=JST))
    async def sunday_purge() -> None:
        now = datetime.now(JST)
        # 0:00 は毎日来る。日曜以外は日付判定へ進まない。
        if now.weekday() != SUNDAY:
            return
        await purge_if_due(bot)

    @sunday_purge.before_loop
    async def wait_until_ready() -> None:
        # 接続前にチャンネルを編集しない。
        await bot.wait_until_ready()

    @sunday_purge.error
    async def on_sunday_error(error: BaseException) -> None:
        logger.exception("日曜削除タスクが失敗しました", exc_info=error)

    sunday_purge.start()
    # タスクオブジェクトが回収されるとループが止まる。
    setattr(bot, "sunday_purge", sunday_purge)
