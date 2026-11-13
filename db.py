"""SQLite への読み書き。Discord には依存しない。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# スキーマ。ギルドIDでパーティを分け、掲示板と削除済みの日曜を別に持つ。
SCHEMA = """
CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    creator_id INTEGER NOT NULL,
    leader_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    party_id INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (party_id, user_id)
);

CREATE TABLE IF NOT EXISTS boards (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_ids TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parties_guild ON parties(guild_id);
CREATE INDEX IF NOT EXISTS idx_members_party ON members(party_id);
"""

LAST_PURGE_KEY = "last_purge_on"


@dataclass(frozen=True)
class Member:
    """パーティに入っている1人。"""

    user_id: int
    joined_at: str


@dataclass(frozen=True)
class Party:
    """1つの募集。members は参加が早い順。"""

    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    creator_id: int
    leader_id: int
    name: str
    note: str | None
    created_at: str
    members: tuple[Member, ...]


@dataclass(frozen=True)
class Board:
    """ギルドに1つだけ置く掲示板。"""

    guild_id: int
    channel_id: int
    message_ids: tuple[int, ...]


class Database:
    """イベントループのスレッドからだけ使う接続。"""

    def __init__(self, path: Path) -> None:
        # 親フォルダが無いと SQLite がファイルを作れない。
        path.parent.mkdir(parents=True, exist_ok=True)
        # トランザクションは自分で BEGIN する。
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # 外部キーを切ると、パーティ削除でメンバーが残る。
        self._conn.execute("PRAGMA foreign_keys = ON")
        enabled = self._conn.execute("PRAGMA foreign_keys").fetchone()
        # 有効になっていなければ削除時の整合が取れない。
        if enabled is None or int(enabled[0]) != 1:
            raise RuntimeError("SQLite の外部キーを有効にできませんでした。")
        self._conn.executescript(SCHEMA)
        # executescript のあとでも、削除時にメンバーが残らないようにし直す。
        self._conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """書き込みを即時ロックして、失敗したら巻き戻す。"""
        # 他の書き込みより先に予約し、人数の同時更新を直列化する。
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            # 途中の INSERT を残さない。
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def load_party(
        self,
        conn: sqlite3.Connection,
        party_id: int,
    ) -> Party | None:
        """ID で1件読む。無ければ None。"""
        row = conn.execute(
            "SELECT * FROM parties WHERE id = ?",
            (party_id,),
        ).fetchone()
        # 削除済みのボタンが押されたときにここへ来る。
        if row is None:
            return None
        return self._party_from_row(conn, row)

    def load_parties_for_guild(
        self,
        conn: sqlite3.Connection,
        guild_id: int,
    ) -> list[Party]:
        """このギルドの募集だけを、作成が早い順で返す。"""
        rows = conn.execute(
            """
            SELECT * FROM parties
            WHERE guild_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (guild_id,),
        ).fetchall()
        return [self._party_from_row(conn, row) for row in rows]

    def load_all_parties(self, conn: sqlite3.Connection) -> list[Party]:
        """日曜削除の前に、全ギルドのスナップショットを取る。"""
        rows = conn.execute(
            "SELECT * FROM parties ORDER BY id ASC",
        ).fetchall()
        return [self._party_from_row(conn, row) for row in rows]

    def find_by_creator(
        self,
        conn: sqlite3.Connection,
        guild_id: int,
        creator_id: int,
    ) -> Party | None:
        """作成者がこのギルドで出している募集を1件返す。"""
        row = conn.execute(
            """
            SELECT * FROM parties
            WHERE guild_id = ? AND creator_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (guild_id, creator_id),
        ).fetchone()
        # まだ作っていなければ削除も再作成もこの結果で分岐する。
        if row is None:
            return None
        return self._party_from_row(conn, row)

    def insert_party(
        self,
        conn: sqlite3.Connection,
        *,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        name: str,
        note: str | None,
        created_at: str,
    ) -> int:
        """カードの message_id は送信後に埋める。戻り値は新しいID。"""
        cursor = conn.execute(
            """
            INSERT INTO parties (
                guild_id, channel_id, message_id, creator_id, leader_id,
                name, note, created_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                creator_id,
                creator_id,
                name,
                note,
                created_at,
            ),
        )
        # ボタンの custom_id に使うため、採番結果が必須。
        if cursor.lastrowid is None:
            raise RuntimeError("パーティIDを採番できませんでした。")
        return int(cursor.lastrowid)

    def insert_member(
        self,
        conn: sqlite3.Connection,
        party_id: int,
        user_id: int,
        joined_at: str,
    ) -> None:
        """参加者を1人追加する。"""
        conn.execute(
            """
            INSERT INTO members (party_id, user_id, joined_at)
            VALUES (?, ?, ?)
            """,
            (party_id, user_id, joined_at),
        )

    def delete_member(
        self,
        conn: sqlite3.Connection,
        party_id: int,
        user_id: int,
    ) -> None:
        """参加者を1人外す。"""
        conn.execute(
            "DELETE FROM members WHERE party_id = ? AND user_id = ?",
            (party_id, user_id),
        )

    def update_note(
        self,
        conn: sqlite3.Connection,
        party_id: int,
        note: str | None,
    ) -> None:
        """募集のメモだけを差し替える。名前とリーダーは変えない。"""
        conn.execute(
            "UPDATE parties SET note = ? WHERE id = ?",
            (note, party_id),
        )

    def update_leader(
        self,
        conn: sqlite3.Connection,
        party_id: int,
        leader_id: int,
    ) -> None:
        """リーダーだけを差し替える。作成者は変えない。"""
        conn.execute(
            "UPDATE parties SET leader_id = ? WHERE id = ?",
            (leader_id, party_id),
        )

    def update_message_id(
        self,
        conn: sqlite3.Connection,
        party_id: int,
        message_id: int,
    ) -> None:
        """公開カードのメッセージIDを保存する。"""
        conn.execute(
            "UPDATE parties SET message_id = ? WHERE id = ?",
            (message_id, party_id),
        )

    def clear_message_id(
        self,
        conn: sqlite3.Connection,
        party_id: int,
    ) -> None:
        """カードが消されていたら、無効なIDを外す。"""
        conn.execute(
            "UPDATE parties SET message_id = NULL WHERE id = ?",
            (party_id,),
        )

    def delete_party(self, conn: sqlite3.Connection, party_id: int) -> None:
        """パーティを消し、外部キーでメンバーも消す。"""
        conn.execute("DELETE FROM parties WHERE id = ?", (party_id,))

    def delete_all_parties(self, conn: sqlite3.Connection) -> None:
        """全ギルドの募集を消す。掲示板の行は残す。"""
        conn.execute("DELETE FROM parties")

    def get_board(self, guild_id: int) -> Board | None:
        """ギルドの掲示板。未設置なら None。"""
        row = self._conn.execute(
            "SELECT guild_id, channel_id, message_ids FROM boards WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        # まだ /party board されていない。
        if row is None:
            return None
        return Board(
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_ids=_parse_message_ids(str(row["message_ids"])),
        )

    def list_boards(self) -> list[Board]:
        """日曜削除で空表示に戻す対象。"""
        rows = self._conn.execute(
            "SELECT guild_id, channel_id, message_ids FROM boards ORDER BY guild_id ASC",
        ).fetchall()
        return [
            Board(
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                message_ids=_parse_message_ids(str(row["message_ids"])),
            )
            for row in rows
        ]

    def save_board(
        self,
        guild_id: int,
        channel_id: int,
        message_ids: list[int],
    ) -> None:
        """掲示板のチャンネルとメッセージIDを上書き保存する。"""
        payload = json.dumps(message_ids)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO boards (guild_id, channel_id, message_ids)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_ids = excluded.message_ids
                """,
                (guild_id, channel_id, payload),
            )

    def delete_board(self, guild_id: int) -> None:
        """掲示板の紐付けだけを消す。"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM boards WHERE guild_id = ?", (guild_id,))

    def get_last_purge(self) -> date | None:
        """最後に処理した日曜。未記録なら None。"""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (LAST_PURGE_KEY,),
        ).fetchone()
        # 初回起動は削除せず、呼び出し側が基準日を書く。
        if row is None:
            return None
        return date.fromisoformat(str(row["value"]))

    def set_last_purge(self, value: date) -> None:
        """処理済みの日曜を記録し、同じ日の二重削除を防ぐ。"""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (LAST_PURGE_KEY, value.isoformat()),
            )

    def _party_from_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Party:
        """行とメンバーを1つの Party にまとめる。"""
        party_id = int(row["id"])
        message_id = row["message_id"]
        note = row["note"]
        return Party(
            id=party_id,
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_id=None if message_id is None else int(message_id),
            creator_id=int(row["creator_id"]),
            leader_id=int(row["leader_id"]),
            name=str(row["name"]),
            note=None if note is None else str(note),
            created_at=str(row["created_at"]),
            members=self._load_members(conn, party_id),
        )

    def _load_members(
        self,
        conn: sqlite3.Connection,
        party_id: int,
    ) -> tuple[Member, ...]:
        """参加時刻、同じ時刻ならユーザーIDの順で読む。"""
        rows = conn.execute(
            """
            SELECT user_id, joined_at FROM members
            WHERE party_id = ?
            ORDER BY joined_at ASC, user_id ASC
            """,
            (party_id,),
        ).fetchall()
        return tuple(
            Member(user_id=int(row["user_id"]), joined_at=str(row["joined_at"]))
            for row in rows
        )


def _parse_message_ids(raw: str) -> tuple[int, ...]:
    """壊れた JSON は空にして、掲示板を置き直せるようにする。"""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    # 配列以外は掲示板のメッセージ一覧として扱わない。
    if not isinstance(parsed, list):
        return ()
    ids: list[int] = []
    for item in parsed:
        # 数値以外が混ざっていたら、その時点で信用しない。
        if isinstance(item, bool) or not isinstance(item, int):
            return ()
        ids.append(item)
    return tuple(ids)
