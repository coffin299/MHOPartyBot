"""Components V2 のカード、一覧、掲示板と参加・退出ボタン。"""

from __future__ import annotations

import logging
import re
from enum import Enum

import discord
from discord import ui

from db import Database, Member, Party
from service import (
    PARTY_LIMIT,
    REASON_EMPTY,
    JoinStatus,
    LeaveStatus,
    clear_card_message,
    guild_lock,
    join_party,
    join_text,
    leave_party,
    leave_text,
    list_parties,
)

logger = logging.getLogger(__name__)

# ユーザーメンションだけ展開し、everyone と役職メンションは鳴らさない。
MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)

# 空き、満員、解散、募集ゼロで枠の色を変える。
OPEN_COLOUR = 0x5865F2
FULL_COLOUR = 0xED4245
CLOSED_COLOUR = 0xED4245
EMPTY_COLOUR = 0x4F545C

# Components V2 は1メッセージ40部品まで。
MAX_COMPONENTS = 40

# TextDisplay 全体の上限4000に、末尾の省略文を足しても超えない目安。
LIST_CHAR_BUDGET = 3800


class _EditResult(Enum):
    """既存メッセージを編集できたか。"""

    UPDATED = "updated"
    MISSING = "missing"
    FAILED = "failed"


def database_from(client: discord.Client) -> Database:
    """ボットにぶら下げた SQLite を取り出す。"""
    database = getattr(client, "db", None)
    # 起動前の呼び出しや、別クライアントからの誤用をここで止める。
    if not isinstance(database, Database):
        raise RuntimeError("データベースが初期化されていません。")
    return database


async def reply_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """操作した人だけに見える返事を、応答済みかどうかで送り分ける。"""
    # すでに応答しているときは追加メッセージにする。
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
        return
    await interaction.response.send_message(content, ephemeral=True)


class JoinButton(
    ui.DynamicItem[ui.Button],
    template=r"^party:join:(?P<party_id>[0-9]+)$",
):
    """再起動後も `party:join:{id}` を受け取る参加ボタン。"""

    def __init__(
        self,
        party_id: int,
        *,
        member_count: int,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            ui.Button(
                label=f"参加 {member_count}/{PARTY_LIMIT}",
                style=discord.ButtonStyle.success,
                custom_id=f"party:join:{party_id}",
                disabled=disabled,
            )
        )
        self.party_id = party_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
    ) -> JoinButton:
        """押されたボタンの custom_id から参加先を復元する。"""
        # 再構築にインタラクション本体は要らない。ID は custom_id にある。
        del interaction
        disabled = bool(getattr(item, "disabled", False))
        # 押した瞬間のラベルは使わず、成功後にデータベースから描き直す。
        return cls(int(match.group("party_id")), member_count=0, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        """参加処理へ渡す。"""
        await _handle_join(interaction, self.party_id)


class LeaveButton(
    ui.DynamicItem[ui.Button],
    template=r"^party:leave:(?P<party_id>[0-9]+)$",
):
    """再起動後も `party:leave:{id}` を受け取る退出ボタン。"""

    def __init__(self, party_id: int) -> None:
        super().__init__(
            ui.Button(
                label="退出",
                style=discord.ButtonStyle.secondary,
                custom_id=f"party:leave:{party_id}",
            )
        )
        self.party_id = party_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
    ) -> LeaveButton:
        """押されたボタンの custom_id から退出先を復元する。"""
        # 退出ボタンに無効状態は持たせていない。
        del interaction, item
        return cls(int(match.group("party_id")))

    async def callback(self, interaction: discord.Interaction) -> None:
        """退出処理へ渡す。"""
        await _handle_leave(interaction, self.party_id)


def build_card(party: Party) -> ui.LayoutView:
    """公開チャンネルに置く、1パーティ分のカード。"""
    view = ui.LayoutView(timeout=None)
    view.add_item(_party_container(party, with_buttons=True))
    return view


def build_closed_view(name: str, reason: str) -> ui.LayoutView:
    """ボタンを外した解散表示。"""
    view = ui.LayoutView(timeout=None)
    view.add_item(
        ui.Container(
            ui.TextDisplay(f"# {sanitize(name)}"),
            _separator(),
            ui.TextDisplay(reason),
            accent_colour=CLOSED_COLOUR,
        )
    )
    return view


def build_list_view(parties: list[Party]) -> ui.LayoutView:
    """このギルドの読み取り専用一覧。募集ごとに枠を分ける。"""
    intro = (
        "# パーティ一覧\n"
        "このサーバーで募集中のパーティです。\n"
        "参加と退出は、パーティのカードか掲示板から行えます。"
    )
    view = ui.LayoutView()
    # 募集が無いときは、灰色の枠一つで空表示にする。
    if not parties:
        view.add_item(
            ui.Container(
                ui.TextDisplay(intro + "\n\n現在募集中のパーティはありません。"),
                accent_colour=EMPTY_COLOUR,
            )
        )
        return view
    view.add_item(ui.TextDisplay(intro))
    used_components = 1
    used_chars = len(intro)
    hidden = 0
    for index, party in enumerate(parties):
        container = _party_container(party, with_buttons=False)
        cost = container._total_count
        chars = _party_char_length(party)
        # 省略案内を足せる余白を残し、40部品と4000文字を超えない。
        if used_components + cost > MAX_COMPONENTS - 1 or used_chars + chars > LIST_CHAR_BUDGET:
            hidden = len(parties) - index
            break
        view.add_item(container)
        used_components += cost
        used_chars += chars
    if hidden:
        suffix = f"他 {hidden} 件は掲示板で確認してください。"
        if used_components < MAX_COMPONENTS and used_chars + len(suffix) <= 4000:
            view.add_item(ui.TextDisplay(suffix))
    return view


def build_board_views(parties: list[Party]) -> list[ui.LayoutView]:
    """掲示板を、40部品に収まるメッセージ単位へ分ける。"""
    # 空でもメッセージは残し、あとから編集できるようにする。
    if not parties:
        view = ui.LayoutView(timeout=None)
        view.add_item(
            ui.Container(
                ui.TextDisplay(_empty_board_text()),
                accent_colour=EMPTY_COLOUR,
            )
        )
        return [view]
    pages: list[list[ui.Container]] = []
    current: list[ui.Container] = []
    # 各ページの先頭に見出しを1部品置く。
    used = 1
    for party in parties:
        container = _party_container(party, with_buttons=True)
        cost = container._total_count
        # この1件を足すと上限を超えるなら、次のメッセージへ回す。
        if current and used + cost > MAX_COMPONENTS:
            pages.append(current)
            current = []
            used = 1
        current.append(container)
        used += cost
    if current:
        pages.append(current)
    views: list[ui.LayoutView] = []
    for page_index, page in enumerate(pages):
        view = ui.LayoutView(timeout=None)
        # 2枚目以降も、単体で見て掲示板だと分かる見出しを置く。
        view.add_item(ui.TextDisplay(_board_header(len(parties), page_index)))
        for container in page:
            view.add_item(container)
        views.append(view)
    return views


async def edit_card(client: discord.Client, party: Party) -> None:
    """公開カードの人数とリーダー表示を最新にする。"""
    # 送信前、またはカードが消えて紐付けを外したあと。
    if party.message_id is None:
        return
    channel = await _messageable(client, party.channel_id)
    # チャンネルが消えていても、募集データ自体は残す。
    if channel is None:
        return
    result = await _edit_partial(channel, party.message_id, build_card(party))
    # カードだけ消されていたら、無効なIDを外して掲示板側のボタンを生かす。
    if result is _EditResult.MISSING:
        clear_missing_card(client, party.id)
        logger.info("パーティカードが無いため紐付けを外しました: %s", party.id)


async def close_card(client: discord.Client, party: Party, reason: str) -> None:
    """解散した募集のカードからボタンを外す。"""
    # カードを出す前に失敗した募集には、閉じるメッセージが無い。
    if party.message_id is None:
        return
    channel = await _messageable(client, party.channel_id)
    if channel is None:
        return
    result = await _edit_partial(
        channel,
        party.message_id,
        build_closed_view(party.name, reason),
    )
    # すでに消えているカードは、解散処理を失敗にしない。
    if result is not _EditResult.UPDATED:
        logger.info(
            "解散表示にできませんでした: party=%s result=%s",
            party.id,
            result.value,
        )


async def refresh_board(client: discord.Client, guild_id: int) -> None:
    """設置済みの掲示板を、今のギルドの募集で描き直す。呼び出し側がロックを持つ。"""
    database = database_from(client)
    board = database.get_board(guild_id)
    # まだ /party board されていないギルドは、カードだけ更新する。
    if board is None:
        return
    views = build_board_views(list_parties(database, guild_id))
    channel = await _messageable(client, board.channel_id)
    # チャンネルが無いときは紐付けを残し、戻ったら次の更新で直す。
    if channel is None:
        logger.warning("掲示板のチャンネルがありません: %s", board.channel_id)
        return
    kept: list[int] = []
    for index, view in enumerate(views):
        previous_id = (
            board.message_ids[index] if index < len(board.message_ids) else None
        )
        # 既存の枚数に収まっている間は、新規投稿せずそのメッセージを書き換える。
        if previous_id is not None:
            result = await _edit_partial(channel, previous_id, view)
            if result is _EditResult.UPDATED or result is _EditResult.FAILED:
                kept.append(previous_id)
                continue
        try:
            message = await channel.send(view=view, allowed_mentions=MENTIONS)
        except discord.HTTPException:
            logger.warning("掲示板メッセージを追加できませんでした: guild=%s", guild_id)
            continue
        kept.append(message.id)
    # 募集が減って余ったメッセージは、掲示板から外す。
    for message_id in board.message_ids:
        if message_id not in kept:
            await _delete_partial(channel, message_id)
    # 1枚も残せなかったときは未設置に戻し、次の /party board を受け付ける。
    if kept:
        database.save_board(guild_id, board.channel_id, kept)
    else:
        database.delete_board(guild_id)


async def remove_placed_board(client: discord.Client, guild_id: int) -> bool:
    """このサーバーの掲示板メッセージを消し、紐付けも外す。無ければ False。"""
    database = database_from(client)
    board = database.get_board(guild_id)
    # 未設置のサーバーでは、消すメッセージが無い。
    if board is None:
        return False
    channel = await _messageable(client, board.channel_id)
    # チャンネルが残っているときだけ、貼ってあるメッセージを消す。
    if channel is not None:
        for message_id in board.message_ids:
            await _delete_partial(channel, message_id)
    else:
        logger.warning(
            "掲示板のチャンネルが無いため、メッセージは消せませんでした: %s",
            board.channel_id,
        )
    # メッセージが残っていても、このサーバーの掲示板としては忘れる。
    database.delete_board(guild_id)
    return True


async def place_board(
    client: discord.Client,
    *,
    guild_id: int,
    channel: discord.abc.Messageable,
) -> None:
    """今のチャンネルへ掲示板を貼り直し、古いメッセージを消す。"""
    database = database_from(client)
    previous = database.get_board(guild_id)
    views = build_board_views(list_parties(database, guild_id))
    sent = await _send_all(channel, views)
    message_ids = [message.id for message in sent]
    channel_id = _channel_id(channel)
    # 先に新しい掲示板を記録してから、古い方を消す。
    database.save_board(guild_id, channel_id, message_ids)
    if previous is None:
        return
    old_channel = await _messageable(client, previous.channel_id)
    if old_channel is None:
        return
    for message_id in previous.message_ids:
        # 同じIDを消すことは無いが、念のため新しい掲示板は残す。
        if message_id in message_ids:
            continue
        await _delete_partial(old_channel, message_id)


def sanitize(value: str) -> str:
    """表示文面から改行と、全体へのメンションを外す。"""
    flattened = " ".join(value.split())
    neutralized = flattened.replace("@everyone", "@\u200beveryone")
    neutralized = neutralized.replace("@here", "@\u200bhere")
    return neutralized.replace("<@&", "<@\u200b&")


def _status_text(party: Party) -> str:
    """リーダー、作成者、メンバー一覧。作成者とリーダーが同じなら作成者は出さない。"""
    lines = [f"**リーダー** <@{party.leader_id}>"]
    # 移譲後だけ、削除権を持つ作成者を別に出す。
    if party.leader_id != party.creator_id:
        lines.append(f"**作成者** <@{party.creator_id}>")
    lines.append(f"**メンバー** {len(party.members)}/{PARTY_LIMIT}")
    for member in _ordered_members(party):
        # 一覧の先頭がリーダーだと、一目で分かるようにする。
        if member.user_id == party.leader_id:
            lines.append(f"- <@{member.user_id}> リーダー")
        else:
            lines.append(f"- <@{member.user_id}>")
    return "\n".join(lines)


def _party_char_length(party: Party) -> int:
    """一覧の文字数制限に使う、この募集の表示文字数。"""
    length = len(sanitize(party.name)) + len(_status_text(party))
    # メモがあるときだけ、引用の分を足す。
    if party.note:
        length += len(sanitize(party.note))
    return length


def clear_missing_card(client: discord.Client, party_id: int) -> None:
    """消えたカードのメッセージIDをデータベースから外す。"""
    clear_card_message(database_from(client), party_id)


async def _handle_join(interaction: discord.Interaction, party_id: int) -> None:
    """参加ボタン。失敗も成功も本人にだけ知らせ、成功時は表示を更新する。"""
    guild_id = interaction.guild_id
    # DM やユーザー不明の操作では、ギルドの募集を動かさない。
    if guild_id is None or interaction.user is None:
        await reply_ephemeral(interaction, "サーバーの中で操作してください。")
        return
    client = interaction.client
    try:
        async with guild_lock(guild_id):
            outcome = join_party(
                database_from(client),
                guild_id=guild_id,
                party_id=party_id,
                user_id=interaction.user.id,
            )
            await reply_ephemeral(interaction, join_text(outcome.status))
            # 人数が変わったときだけ、カードと掲示板を描き直す。
            if outcome.status is JoinStatus.JOINED and outcome.party is not None:
                await edit_card(client, outcome.party)
                await refresh_board(client, guild_id)
    except Exception:
        logger.exception("参加処理に失敗しました: party=%s", party_id)
        # 成功メッセージを出した後の失敗は、ログだけに留める。
        if not interaction.response.is_done():
            await reply_ephemeral(
                interaction,
                "処理に失敗しました。時間をおいてもう一度押してください。",
            )


async def _handle_leave(interaction: discord.Interaction, party_id: int) -> None:
    """退出ボタン。リーダーなら移譲、無人なら解散として表示を更新する。"""
    guild_id = interaction.guild_id
    if guild_id is None or interaction.user is None:
        await reply_ephemeral(interaction, "サーバーの中で操作してください。")
        return
    client = interaction.client
    try:
        async with guild_lock(guild_id):
            outcome = leave_party(
                database_from(client),
                guild_id=guild_id,
                party_id=party_id,
                user_id=interaction.user.id,
            )
            await reply_ephemeral(interaction, leave_text(outcome.status))
            # 無人になったカードは、募集表示のまま残さない。
            if outcome.status is LeaveStatus.DISBANDED and outcome.disbanded is not None:
                await close_card(client, outcome.disbanded, REASON_EMPTY)
            elif outcome.party is not None:
                await edit_card(client, outcome.party)
            # 失敗（未参加など）では掲示板を動かさない。
            if outcome.status in (
                LeaveStatus.LEFT,
                LeaveStatus.TRANSFERRED,
                LeaveStatus.DISBANDED,
            ):
                await refresh_board(client, guild_id)
    except Exception:
        logger.exception("退出処理に失敗しました: party=%s", party_id)
        if not interaction.response.is_done():
            await reply_ephemeral(
                interaction,
                "処理に失敗しました。時間をおいてもう一度押してください。",
            )


def _separator() -> ui.Separator:
    """見出し、メモ、メンバーの間に置く細い区切り。"""
    return ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)


def _party_colour(party: Party) -> int:
    """空きは青、満員は赤。"""
    if len(party.members) >= PARTY_LIMIT:
        return FULL_COLOUR
    return OPEN_COLOUR


def _party_container(party: Party, *, with_buttons: bool) -> ui.Container:
    """見出し、メモ、メンバーを区切った枠。ボタンはカードと掲示板だけに付ける。"""
    children: list[ui.Item] = [
        ui.TextDisplay(f"# {sanitize(party.name)}"),
        _separator(),
    ]
    # メモが無い募集は、空の引用を置かずにメンバーへ進む。
    if party.note:
        children.append(ui.TextDisplay(f"> {sanitize(party.note)}"))
        children.append(_separator())
    children.append(ui.TextDisplay(_status_text(party)))
    if with_buttons:
        full = len(party.members) >= PARTY_LIMIT
        children.append(
            ui.ActionRow(
                # 満員のときは押せなくするが、人数はラベルに残す。
                JoinButton(
                    party.id,
                    member_count=len(party.members),
                    disabled=full,
                ),
                LeaveButton(party.id),
            )
        )
    return ui.Container(*children, accent_colour=_party_colour(party))


def _ordered_members(party: Party) -> list[Member]:
    """リーダーを先頭にし、それ以外は参加が早い順に並べる。"""
    return sorted(
        party.members,
        key=lambda member: (
            member.user_id != party.leader_id,
            member.joined_at,
            member.user_id,
        ),
    )


def _empty_board_text() -> str:
    """募集が0件の掲示板。"""
    return (
        "# パーティ掲示板\n"
        "現在募集中のパーティはありません。\n"
        "毎週日曜 0:00（JST）に、すべてのパーティは解散します。"
    )


def _board_header(total: int, page_index: int) -> str:
    """掲示板のページ見出し。先頭ページだけ件数とリセット時刻を出す。"""
    if page_index == 0:
        return (
            "# パーティ掲示板\n"
            f"このサーバーの募集は {total} 件です。\n"
            "毎週日曜 0:00（JST）に、すべてのパーティは解散します。"
        )
    return f"# パーティ掲示板（{page_index + 1}）"


async def _messageable(
    client: discord.Client,
    channel_id: int,
) -> discord.abc.Messageable | None:
    """キャッシュに無ければ API でチャンネルを取り、投稿できるものだけ返す。"""
    channel = client.get_channel(channel_id)
    # 再起動直後はキャッシュが空なので、ID から取りにいく。
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            logger.warning("チャンネルを取得できませんでした: %s", channel_id)
            return None
    # カテゴリなど、メッセージを置けないチャンネルは対象外。
    if isinstance(channel, discord.abc.Messageable):
        return channel
    logger.warning("メッセージを送れないチャンネルです: %s", channel_id)
    return None


async def _edit_partial(
    channel: discord.abc.Messageable,
    message_id: int,
    view: ui.LayoutView,
) -> _EditResult:
    """履歴を読まず、ボット自身のメッセージを Components V2 で書き換える。"""
    partial = channel.get_partial_message(message_id)
    try:
        await partial.edit(view=view, allowed_mentions=MENTIONS)
    except discord.NotFound:
        return _EditResult.MISSING
    except discord.HTTPException:
        logger.warning("メッセージを更新できませんでした: %s", message_id)
        return _EditResult.FAILED
    return _EditResult.UPDATED


async def _delete_partial(
    channel: discord.abc.Messageable,
    message_id: int,
) -> None:
    """ボット自身のメッセージを消す。すでに無い場合は成功とみなす。"""
    partial = channel.get_partial_message(message_id)
    try:
        await partial.delete()
    except discord.NotFound:
        return
    except discord.HTTPException:
        logger.info("メッセージを削除できませんでした: %s", message_id)


async def _send_all(
    channel: discord.abc.Messageable,
    views: list[ui.LayoutView],
) -> list[discord.Message]:
    """途中で失敗したら、その回に送った分を消して例外を戻す。"""
    sent: list[discord.Message] = []
    try:
        for view in views:
            sent.append(await channel.send(view=view, allowed_mentions=MENTIONS))
    except discord.HTTPException:
        for message in sent:
            try:
                await message.delete()
            except discord.HTTPException:
                logger.info("送信に失敗した掲示板を消せませんでした: %s", message.id)
        raise
    return sent


def _channel_id(channel: discord.abc.Messageable) -> int:
    """Messageable からチャンネルIDを取り出す。"""
    channel_id = getattr(channel, "id", None)
    # ここに来るチャンネルはギルドのテキストチャンネル想定。
    if not isinstance(channel_id, int):
        raise RuntimeError("チャンネルIDを取得できませんでした。")
    return channel_id
