"""初回起動時に config.yaml を用意し、そこから設定を読む。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

# プロジェクト直下を基準にして、起動ディレクトリに依存しない。
ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.default.yaml"
CONFIG_PATH = ROOT / "config.yaml"


@dataclass(frozen=True)
class Settings:
    """ボットの起動に必要な設定。"""

    token: str
    database_path: Path
    dev_guild_id: int | None


def load_settings() -> Settings:
    """config.yaml が無ければ規定ファイルから作り、その内容を返す。"""
    _ensure_config_file()
    loaded = _read_yaml(CONFIG_PATH)
    # ファイルの形が辞書でなければ、どの項目かも分からない。
    if not isinstance(loaded, dict):
        raise SystemExit("config.yaml の先頭はキーと値の組にしてください。")
    token = _required_text(loaded, "discord_token")
    database_path = _database_path(loaded.get("database_path"))
    dev_guild_id = _optional_guild_id(loaded.get("dev_guild_id"))
    return Settings(
        token=token,
        database_path=database_path,
        dev_guild_id=dev_guild_id,
    )


def _ensure_config_file() -> None:
    """未作成のときだけ、規定ファイルを config.yaml へコピーする。"""
    # 2回目以降は、利用者が編集したファイルを上書きしない。
    if CONFIG_PATH.exists():
        return
    # テンプレートが無いと、空の設定ファイルを作れない。
    if not DEFAULT_CONFIG_PATH.exists():
        raise SystemExit("config.default.yaml がありません。")
    shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)


def _read_yaml(path: Path) -> object:
    """UTF-8 の YAML を読む。壊れていれば起動を止める。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"config.yaml を読めませんでした: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SystemExit(f"config.yaml の形式が正しくありません: {exc}") from exc


def _required_text(loaded: dict[object, object], key: str) -> str:
    """文字列の設定を取り出す。未入力は空文字にする。"""
    value = loaded.get(key, "")
    # 空欄や null は、あとでトークン未設定として扱う。
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SystemExit(f"config.yaml の {key} は文字列にしてください。")
    return value.strip()


def _database_path(value: object) -> Path:
    """データベースパスをプロジェクト直下の絶対パスにする。"""
    # 省略と空欄は、規定の SQLite ファイルに戻す。
    if value is None or value == "":
        raw = "data/party.db"
    elif isinstance(value, str):
        raw = value.strip() or "data/party.db"
    else:
        raise SystemExit("config.yaml の database_path は文字列にしてください。")
    path = Path(raw)
    # 相対パスは、起動した場所ではなくプロジェクト直下から辿る。
    if not path.is_absolute():
        path = ROOT / path
    return path


def _optional_guild_id(value: object) -> int | None:
    """開発用ギルドID。null ならグローバル同期、不正なら起動を止める。"""
    # 未設定は本番向けのグローバル同期。
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemExit("config.yaml の dev_guild_id は整数か null にしてください。")
    # 0 以下はギルドIDとして無効。
    if value <= 0:
        raise SystemExit("config.yaml の dev_guild_id は正の整数にしてください。")
    return value
