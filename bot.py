"""4人パーティ募集ボットの起動口。"""

import sys

# サブモジュールを読む前に切り、この起動では .pyc を残さない。
sys.dont_write_bytecode = True

import logging

import discord
from discord.ext import commands

from config import Settings, load_settings
from db import Database
from tasks import purge_if_due, start_sunday_task
from views import JoinButton, LeaveButton

logger = logging.getLogger(__name__)


class PartyBot(commands.Bot):
    """スラッシュコマンド、SQLite、日曜削除を持つボット。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
        )
        self.settings = settings
        # 参加ボタンより先に、同じプロセスから触れる接続を作る。
        self.db = Database(settings.database_path)

    async def setup_hook(self) -> None:
        """Cog、永続ボタン、コマンド同期、日曜タスクを準備する。"""
        await self.load_extension("cogs.party")
        # 再起動後の参加・退出は、メッセージ側の View が無くてもここへ来る。
        self.add_dynamic_items(JoinButton, LeaveButton)
        await self._sync_commands()
        start_sunday_task(self)
        logger.info("データベースを開きました: %s", self.settings.database_path)

    async def on_ready(self) -> None:
        """ログインのたびに、停止中へ過ぎた日曜を1回だけ確認する。"""
        logger.info("ログインしました: %s", self.user)
        await purge_if_due(self)

    async def _sync_commands(self) -> None:
        """開発ギルドがあればそこだけ即時同期し、無ければグローバル同期する。"""
        dev_guild_id = self.settings.dev_guild_id
        # ギルド同期は反映が速い。指定が無いときだけ全体へ出す。
        if dev_guild_id is not None:
            guild = discord.Object(id=dev_guild_id)
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("ギルド %s に %s 件同期しました", dev_guild_id, len(synced))
            return
        synced = await self.tree.sync()
        logger.info("グローバルに %s 件同期しました", len(synced))


def main() -> None:
    """設定を読んでボットを起動する。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings()
    # トークン無しでは Discord へ繋がない。
    if not settings.token:
        raise SystemExit("discord_token が未設定です。config.yaml を確認してください。")
    bot = PartyBot(settings)
    bot.run(settings.token)


if __name__ == "__main__":
    main()
