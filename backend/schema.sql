PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS voca_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  base_word TEXT NOT NULL,
  target_pos TEXT NOT NULL,
  use_word TEXT NOT NULL,
  ipa TEXT,
  zh_meaning TEXT NOT NULL,
  example_en TEXT,
  example_zh TEXT,
  phrases TEXT,
  note TEXT,
  upload_date TEXT NOT NULL,
  source_file TEXT,
  is_answerable INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_voca_item
ON voca_item(base_word, target_pos, use_word);

CREATE INDEX IF NOT EXISTS ix_voca_item_answerable
ON voca_item(is_answerable);

CREATE TABLE IF NOT EXISTS user_item_stat (
  user_id TEXT NOT NULL,
  voca_item_id INTEGER NOT NULL,
  total_attempts INTEGER NOT NULL DEFAULT 0,
  wrong_count INTEGER NOT NULL DEFAULT 0,
  correct_count INTEGER NOT NULL DEFAULT 0,
  error_rate REAL NOT NULL DEFAULT 0,
  last_seen_date TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, voca_item_id),
  FOREIGN KEY (voca_item_id) REFERENCES voca_item(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_user_item_stat_err
ON user_item_stat(user_id, error_rate DESC);

CREATE INDEX IF NOT EXISTS ix_user_item_stat_seen
ON user_item_stat(user_id, last_seen_date);
