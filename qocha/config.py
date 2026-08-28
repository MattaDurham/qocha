"""Configuration for a Qocha vault.

Everything is explicit constructor arguments with sensible defaults; a
`qocha.json` at the vault root can override any field so a vault carries
its own settings. Nothing reads global state.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

# Directories never scanned, regardless of `dirs`.
EXCLUDE_DIRS = {".obsidian", ".trash", ".git", ".qocha", ".venv",
                "node_modules", "__pycache__"}

CONFIG_FILE = "qocha.json"

# Fields a vault-root qocha.json may set.
_JSON_FIELDS = ("dirs", "owner", "db", "ollama_url", "embed_model",
                "answer_model", "raw_dir")


@dataclass
class Config:
    root: Path
    dirs: list | None = None          # None = the whole vault, recursively
    db: Path | None = None            # default: <root>/.qocha/index.sqlite
    # Where the immutable source corpus lives, vault-relative. "." means
    # the sources ARE the vault root — an attached folder that already
    # held files before qocha saw it, which must never be reorganized
    # into a raw/ subdirectory. Persisted so lint, preflight and the
    # commissioning survey all agree about which files are sources.
    raw_dir: str = "raw"
    owner: str = "the owner"
    ollama_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    answer_model: str = "sonnet"
    exclude_dirs: set = field(default_factory=lambda: set(EXCLUDE_DIRS))

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()
        if self.db is None:
            self.db = self.root / ".qocha" / "index.sqlite"
        self.db = Path(self.db).expanduser()

    @classmethod
    def load(cls, root, **overrides):
        """Config for `root`, merging <root>/qocha.json under `overrides`."""
        root = Path(root).expanduser()
        values = {}
        cfg_path = root / CONFIG_FILE
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text())
            except (OSError, ValueError):
                data = {}
            for k in _JSON_FIELDS:
                if k in data and data[k] is not None:
                    values[k] = data[k]
        for k, v in overrides.items():
            if v is not None:
                values[k] = v
        return cls(root=root, **values)
