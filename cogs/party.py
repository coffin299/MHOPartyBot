""" `/party` のスラッシュコマンド。"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from service import (
    REASON_CREATOR,
    CreateStatus,
    DeleteStatus,
    attach_card_message,
    create_party,
    create_text,
    delete_created_party,
    delete_text,
    discard_party,
    guild_lock,
    list_parties,
)
from views import (
    MENTIONS,
    build_card,
    build_list_view,
    close_card,
    database_from,
    place_board,
    refresh_board,
    reply_ephemeral,
)

logger = logging.getLogger(__name__)


class PartyCog(commands.Cog):
    """4人パーティの作成、削除、一覧、掲示板。"""

    party = app_commands.Group(name="party", description="4人パーティの募集")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @party.command(name="create", description="パーティを作成する")
    @app_commands.describe(
        name="パーティ名（80文字以内）",
        note="メモ（任意、300文字以内）",
    )
    @app_commands.guild_only()
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        note: str | None = None,
    ) -> None:
        """公開カードを出し、作成者をリーダーにする。応答は全員に見える。"""
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        # DM では募集を作らない。guild_only でもここでもう一度見る。
        if guild_id is None or channel_id is None or interaction.user is None:
            await reply_ephemeral(interaction, "サーバーの中で実行してください。")
            return
        database = database_from(self.bot)
        async with guild_lock(guild_id):
            outcome = create_party(
                database,
                guild_id=guild_id,
                channel_id=channel_id,
                creator_id=interaction.user.id,
                name=name,
                note=note,
            )
            # バリデーション失敗と作成済みは、本人にだけ返す。
            if outcome.status is not CreateStatus.CREATED or outcome.party is None:
                await reply_ephemeral(interaction, create_text(outcome.status))
                return
            party = outcome.party
            try:
                # 成功時の本体は、このチャンネルに残るカード。
                await interaction.response.send_message(
                    view=build_card(party),
                    ephemeral=False,
                    allowed_mentions=MENTIONS,
                )
            except discord.HTTPException:
                # カードが無い募集を残すと、削除するまで再作成できない。
                discard_party(database, party.id)
                logger.exception("パーティカードを送信できませんでした: %s", party.id)
                if not interaction.response.is_done():
                    await reply_ephemeral(
                        interaction,
                        "パーティカードを送信できませんでした。",
                    )
                return
            try:
                message = await interaction.original_response()
                # 参加・退出のあと、このメッセージを編集する。
                attach_card_message(database, party.id, message.id)
            except discord.HTTPException:
                logger.exception("パーティカードを紐付けできませんでした: %s", party.id)
            await refresh_board(self.bot, guild_id)

    @party.command(name="delete", description="自分が作成したパーティを削除する")
    @app_commands.guild_only()
    async def delete(self, interaction: discord.Interaction) -> None:
        """作成者だけが、引数なしで自分の募集を消す。返事は本人だけ。"""
        guild_id = interaction.guild_id
        if guild_id is None or interaction.user is None:
            await reply_ephemeral(interaction, "サーバーの中で実行してください。")
            return
        async with guild_lock(guild_id):
            outcome = delete_created_party(
                database_from(self.bot),
                guild_id=guild_id,
                creator_id=interaction.user.id,
            )
            # 成否は実行者だけに見せる。
            await reply_ephemeral(interaction, delete_text(outcome.status))
            # 削除できたときだけ、公開カードと掲示板を閉じる。
            if outcome.status is not DeleteStatus.DELETED or outcome.party is None:
                return
            await close_card(self.bot, outcome.party, REASON_CREATOR)
            await refresh_board(self.bot, guild_id)

    @party.command(name="list", description="このサーバーのパーティ一覧を表示する")
    @app_commands.describe(ephemeral="自分だけに表示する（規定値: オン）")
    @app_commands.guild_only()
    async def party_list(
        self,
        interaction: discord.Interaction,
        ephemeral: bool = True,
    ) -> None:
        """このギルドの募集だけを Components V2 で出す。規定では本人だけ。"""
        guild_id = interaction.guild_id
        if guild_id is None:
            await reply_ephemeral(interaction, "サーバーの中で実行してください。")
            return
        parties = list_parties(database_from(self.bot), guild_id)
        # false のときだけ、今いるチャンネルの全員に見える。
        await interaction.response.send_message(
            view=build_list_view(parties),
            ephemeral=ephemeral,
            allowed_mentions=MENTIONS,
        )

    @party.command(name="board", description="パーティ掲示板をこのチャンネルに設置する")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    async def board(self, interaction: discord.Interaction) -> None:
        """今のチャンネルへ掲示板を貼り、以後の募集更新で編集し続ける。"""
        guild_id = interaction.guild_id
        channel = interaction.channel
        # スレッドやテキストチャンネル以外には貼らない。
        if guild_id is None or not isinstance(channel, discord.abc.Messageable):
            await reply_ephemeral(interaction, "このチャンネルには掲示板を置けません。")
            return
        # 古い掲示板の削除と複数枚の送信が3秒を超えてもよいように、先に受ける。
        await interaction.response.defer(ephemeral=True)
        async with guild_lock(guild_id):
            try:
                await place_board(self.bot, guild_id=guild_id, channel=channel)
            except discord.HTTPException:
                logger.exception("掲示板を投稿できませんでした: guild=%s", guild_id)
                await reply_ephemeral(
                    interaction,
                    "掲示板を投稿できませんでした。ボットの権限を確認してください。",
                )
                return
        await reply_ephemeral(interaction, "掲示板をこのチャンネルに設置しました。")

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """チェック失敗と予期しない例外を、本人向けの短い文にする。"""
        original = getattr(error, "original", error)
        # サーバー外ではコマンド自体を進めない。
        if isinstance(original, app_commands.NoPrivateMessage):
            await reply_ephemeral(interaction, "サーバーの中で実行してください。")
            return
        # 掲示板に必要なチャンネル権限が足りない。
        if isinstance(original, app_commands.BotMissingPermissions):
            await reply_ephemeral(
                interaction,
                "ボットに、チャンネルの閲覧、メッセージ送信、メッセージ履歴の閲覧が必要です。",
            )
            return
        logger.exception("パーティコマンドに失敗しました", exc_info=original)
        await reply_ephemeral(
            interaction,
            "処理に失敗しました。時間をおいてもう一度実行してください。",
        )


async def setup(bot: commands.Bot) -> None:
    """Cog を読み込む。"""
    await bot.add_cog(PartyCog(bot))
