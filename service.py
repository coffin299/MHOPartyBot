"""パーティの作成、参加、退出、移譲、メモ更新、削除。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from db import Database, Party

# 募集の上限と入力長。表示と検査の両方でこの値を使う。
PARTY_LIMIT = 4
NAME_MAX = 80
NOTE_MAX = 300

# カードをボタンなしの解散表示へ変えるときの文言。
REASON_CREATOR = "作成者が解散しました。"
REASON_EMPTY = "メンバーがいなくなったため解散しました。"
REASON_SUNDAY = "日曜リセットで解散しました。"

# ギルドごとの表示更新を重ねないためのロック。
_guild_locks: dict[int, asyncio.Lock] = {}


class CreateStatus(Enum):
    """作成の結果。"""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    EMPTY_NAME = "empty_name"
    NAME_TOO_LONG = "name_too_long"
    NOTE_TOO_LONG = "note_too_long"


class JoinStatus(Enum):
    """参加の結果。"""

    JOINED = "joined"
    ALREADY_MEMBER = "already_member"
    FULL = "full"
    NOT_FOUND = "not_found"
    WRONG_GUILD = "wrong_guild"


class LeaveStatus(Enum):
    """退出の結果。"""

    LEFT = "left"
    TRANSFERRED = "transferred"
    DISBANDED = "disbanded"
    NOT_MEMBER = "not_member"
    NOT_FOUND = "not_found"
    WRONG_GUILD = "wrong_guild"


class DeleteStatus(Enum):
    """作成者による削除の結果。"""

    DELETED = "deleted"
    NOT_FOUND = "not_found"


class ModifyStatus(Enum):
    """メモ更新の結果。今のリーダーだけが編集できる。"""

    UPDATED = "updated"
    NOT_FOUND = "not_found"
    NOTE_TOO_LONG = "note_too_long"
    MULTIPLE = "multiple"


@dataclass(frozen=True)
class CreateOutcome:
    """作成に成功したときだけ party を持つ。"""

    status: CreateStatus
    party: Party | None


@dataclass(frozen=True)
class JoinOutcome:
    """参加できなかったときも、表示中の party を返すことがある。"""

    status: JoinStatus
    party: Party | None


@dataclass(frozen=True)
class LeaveOutcome:
    """disbanded は解散前のカード位置。party は残っている募集。"""

    status: LeaveStatus
    party: Party | None
    disbanded: Party | None
    new_leader_id: int | None


@dataclass(frozen=True)
class DeleteOutcome:
    """削除した募集のカードを閉じるためのスナップショット。"""

    status: DeleteStatus
    party: Party | None


@dataclass(frozen=True)
class ModifyOutcome:
    """メモを更新したあとの募集。失敗時は party を持たない。"""

    status: ModifyStatus
    party: Party | None


def guild_lock(guild_id: int) -> asyncio.Lock:
    """同じギルドの募集更新を1本ずつ通す。"""
    lock = _guild_locks.get(guild_id)
    # 初回だけ作る。イベントループは単スレッドなのでここは競合しない。
    if lock is None:
        lock = asyncio.Lock()
        _guild_locks[guild_id] = lock
    return lock


def utc_now() -> str:
    """参加順の比較に使える UTC の文字列を返す。"""
    return datetime.now(timezone.utc).isoformat()


def create_party(
    database: Database,
    *,
    guild_id: int,
    channel_id: int,
    creator_id: int,
    name: str,
    note: str | None,
) -> CreateOutcome:
    """作成者をリーダー兼メンバーにして、ギルド内で1件だけ作る。"""
    cleaned_name = " ".join(name.split())
    # 空白だけは名前として受け付けない。
    if not cleaned_name:
        return CreateOutcome(CreateStatus.EMPTY_NAME, None)
    # カードの見出しが崩れない長さで切る。
    if len(cleaned_name) > NAME_MAX:
        return CreateOutcome(CreateStatus.NAME_TOO_LONG, None)
    cleaned_note = _clean_note(note)
    # 任意メモも表示幅を超えたら拒否する。
    if cleaned_note is not None and len(cleaned_note) > NOTE_MAX:
        return CreateOutcome(CreateStatus.NOTE_TOO_LONG, None)
    created_at = utc_now()
    with database.transaction() as conn:
        existing = database.find_by_creator(conn, guild_id, creator_id)
        # 削除権を引数なしの delete に寄せるため、作成中は1人1件。
        if existing is not None:
            return CreateOutcome(CreateStatus.ALREADY_EXISTS, existing)
        party_id = database.insert_party(
            conn,
            guild_id=guild_id,
            channel_id=channel_id,
            creator_id=creator_id,
            name=cleaned_name,
            note=cleaned_note,
            created_at=created_at,
        )
        # 作成者は最初から枠を1つ使う。
        database.insert_member(conn, party_id, creator_id, created_at)
        party = database.load_party(conn, party_id)
    # INSERT 直後なので、ここで消えているのは異常。
    if party is None:
        raise RuntimeError("作成したパーティを読み戻せませんでした。")
    return CreateOutcome(CreateStatus.CREATED, party)


def attach_card_message(
    database: Database,
    party_id: int,
    message_id: int,
) -> None:
    """送信できた公開カードを、後から編集できるよう紐付ける。"""
    with database.transaction() as conn:
        database.update_message_id(conn, party_id, message_id)


def clear_card_message(database: Database, party_id: int) -> None:
    """チャンネルから消されたカードのIDを忘れる。"""
    with database.transaction() as conn:
        database.clear_message_id(conn, party_id)


def discard_party(database: Database, party_id: int) -> None:
    """カードを送れなかった作成を取り消す。"""
    with database.transaction() as conn:
        database.delete_party(conn, party_id)


def join_party(
    database: Database,
    *,
    guild_id: int,
    party_id: int,
    user_id: int,
) -> JoinOutcome:
    """空いているときだけ参加者を足す。"""
    with database.transaction() as conn:
        party = database.load_party(conn, party_id)
        # 解散後のボタンは何もしない。
        if party is None:
            return JoinOutcome(JoinStatus.NOT_FOUND, None)
        # 別サーバーのボタンをこのギルドの操作として扱わない。
        if party.guild_id != guild_id:
            return JoinOutcome(JoinStatus.WRONG_GUILD, None)
        # 二度押しで枠を二重に消費しない。
        if any(member.user_id == user_id for member in party.members):
            return JoinOutcome(JoinStatus.ALREADY_MEMBER, party)
        # トランザクション内で数え、5人目を入れない。
        if len(party.members) >= PARTY_LIMIT:
            return JoinOutcome(JoinStatus.FULL, party)
        database.insert_member(conn, party_id, user_id, utc_now())
        updated = database.load_party(conn, party_id)
    return JoinOutcome(JoinStatus.JOINED, updated)


def leave_party(
    database: Database,
    *,
    guild_id: int,
    party_id: int,
    user_id: int,
) -> LeaveOutcome:
    """退出する。リーダーなら最古の残りメンバーへ移譲し、無人なら削除する。"""
    with database.transaction() as conn:
        party = database.load_party(conn, party_id)
        # すでに無い募集の退出ボタン。
        if party is None:
            return LeaveOutcome(LeaveStatus.NOT_FOUND, None, None, None)
        # ギルドをまたいだ退出は拒否する。
        if party.guild_id != guild_id:
            return LeaveOutcome(LeaveStatus.WRONG_GUILD, None, None, None)
        # 参加していない人の退出は状態を変えない。
        if not any(member.user_id == user_id for member in party.members):
            return LeaveOutcome(LeaveStatus.NOT_MEMBER, party, None, None)
        database.delete_member(conn, party_id, user_id)
        updated = database.load_party(conn, party_id)
        # 読み戻しに失敗したら、人数ゼロと同じく募集を終わらせる。
        if updated is None or not updated.members:
            database.delete_party(conn, party_id)
            return LeaveOutcome(LeaveStatus.DISBANDED, None, party, None)
        # リーダー以外の退出では権限を動かさない。
        if party.leader_id != user_id:
            return LeaveOutcome(LeaveStatus.LEFT, updated, None, None)
        # 参加が早い人を優先し、同時刻だけユーザーIDで安定させる。
        successor = min(
            updated.members,
            key=lambda member: (member.joined_at, member.user_id),
        )
        database.update_leader(conn, party_id, successor.user_id)
        transferred = database.load_party(conn, party_id)
    return LeaveOutcome(
        LeaveStatus.TRANSFERRED,
        transferred,
        None,
        successor.user_id,
    )


def modify_note(
    database: Database,
    *,
    guild_id: int,
    user_id: int,
    note: str | None,
) -> ModifyOutcome:
    """今のリーダーが、メモだけを書き換える。空欄はメモなしにする。"""
    cleaned_note = _clean_note(note)
    # 表示が崩れる長さは、作成時と同じ基準で拒む。
    if cleaned_note is not None and len(cleaned_note) > NOTE_MAX:
        return ModifyOutcome(ModifyStatus.NOTE_TOO_LONG, None)
    with database.transaction() as conn:
        parties = database.load_parties_for_guild(conn, guild_id)
        # 移譲後は新しいリーダーだけがメモを編集できる。
        editable = [party for party in parties if party.leader_id == user_id]
        # リーダーをしている募集がこのサーバーに無い。
        if not editable:
            return ModifyOutcome(ModifyStatus.NOT_FOUND, None)
        # リーダーが複数の募集にいるときは、どれを更新するか決められない。
        if len(editable) > 1:
            return ModifyOutcome(ModifyStatus.MULTIPLE, None)
        party = editable[0]
        database.update_note(conn, party.id, cleaned_note)
        updated = database.load_party(conn, party.id)
    return ModifyOutcome(ModifyStatus.UPDATED, updated)


def delete_created_party(
    database: Database,
    *,
    guild_id: int,
    creator_id: int,
) -> DeleteOutcome:
    """作成者本人の募集を1件消す。リーダーが移っていても作成者なら消せる。"""
    with database.transaction() as conn:
        party = database.find_by_creator(conn, guild_id, creator_id)
        # 削除できる募集が無い。
        if party is None:
            return DeleteOutcome(DeleteStatus.NOT_FOUND, None)
        database.delete_party(conn, party.id)
    return DeleteOutcome(DeleteStatus.DELETED, party)


def list_parties(database: Database, guild_id: int) -> list[Party]:
    """一覧と掲示板用に、このギルドの募集だけ返す。"""
    with database.transaction() as conn:
        return database.load_parties_for_guild(conn, guild_id)


def purge_all_parties(database: Database) -> list[Party]:
    """削除前の一覧を返し、全ギルドの募集を消す。"""
    with database.transaction() as conn:
        parties = database.load_all_parties(conn)
        database.delete_all_parties(conn)
    return parties


def create_text(status: CreateStatus) -> str:
    """作成失敗を、実行した人だけに返す文言にする。"""
    # 成功時は公開カードが本体なので、ここは失敗専用。
    if status is CreateStatus.ALREADY_EXISTS:
        return (
            "このサーバーでは、すでにパーティを作成しています。"
            "削除してから作り直してください。"
        )
    if status is CreateStatus.EMPTY_NAME:
        return "パーティ名を入力してください。"
    if status is CreateStatus.NAME_TOO_LONG:
        return f"パーティ名は{NAME_MAX}文字以内にしてください。"
    if status is CreateStatus.NOTE_TOO_LONG:
        return f"メモは{NOTE_MAX}文字以内にしてください。"
    return "パーティを作成できませんでした。"


def join_text(status: JoinStatus) -> str:
    """参加ボタンの結果文言。"""
    if status is JoinStatus.JOINED:
        return "参加しました。"
    if status is JoinStatus.ALREADY_MEMBER:
        return "すでに参加しています。"
    if status is JoinStatus.FULL:
        return "満員です。"
    if status is JoinStatus.NOT_FOUND:
        return "このパーティはもうありません。"
    if status is JoinStatus.WRONG_GUILD:
        return "別のサーバーのパーティです。"
    return "参加できませんでした。"


def leave_text(status: LeaveStatus) -> str:
    """退出ボタンの結果文言。"""
    if status is LeaveStatus.LEFT:
        return "退出しました。"
    if status is LeaveStatus.TRANSFERRED:
        return "退出しました。リーダーを次のメンバーに移しました。"
    if status is LeaveStatus.DISBANDED:
        return "退出しました。メンバーがいないためパーティを解散しました。"
    if status is LeaveStatus.NOT_MEMBER:
        return "このパーティに参加していません。"
    if status is LeaveStatus.NOT_FOUND:
        return "このパーティはもうありません。"
    if status is LeaveStatus.WRONG_GUILD:
        return "別のサーバーのパーティです。"
    return "退出できませんでした。"


def modify_text(status: ModifyStatus) -> str:
    """メモ更新の結果文言。成功も失敗も実行者だけに返す。"""
    if status is ModifyStatus.UPDATED:
        return "メモを更新しました。"
    if status is ModifyStatus.NOT_FOUND:
        return "リーダーをしているパーティがありません。"
    if status is ModifyStatus.NOTE_TOO_LONG:
        return f"メモは{NOTE_MAX}文字以内にしてください。"
    if status is ModifyStatus.MULTIPLE:
        return "リーダーのパーティが複数あります。1件だけにしてから実行してください。"
    return "メモを更新できませんでした。"


def delete_text(status: DeleteStatus) -> str:
    """削除コマンドの結果文言。"""
    if status is DeleteStatus.DELETED:
        return "パーティを削除しました。"
    if status is DeleteStatus.NOT_FOUND:
        return "削除できるパーティがありません。"
    return "パーティを削除できませんでした。"


def _clean_note(note: str | None) -> str | None:
    """空のメモは未入力と同じにする。"""
    # 省略された任意引数。
    if note is None:
        return None
    cleaned = " ".join(note.split())
    # 空白だけならメモなしで保存する。
    if not cleaned:
        return None
    return cleaned
