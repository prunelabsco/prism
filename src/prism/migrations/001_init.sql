-- Initial schema

CREATE TABLE reels (
  id            TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  url           TEXT NOT NULL,
  title         TEXT,
  user_note     TEXT,
  saved_at      TEXT,
  status        TEXT NOT NULL,
  stage_error   TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  duplicate_of  TEXT REFERENCES reels(id),
  summary       TEXT,
  card_line     TEXT,
  note_path     TEXT,
  source_tags   TEXT,  -- JSON list from the source (IG collection names)
  export_hash   TEXT,  -- sha256 of the note as last written by prism
  user_status   TEXT DEFAULT 'new',

  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (source, source_id)
);

CREATE TABLE categories (
  slug          TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  description   TEXT NOT NULL,
  status        TEXT NOT NULL,
  proposed_by   TEXT,
  reason        TEXT,
  sort_order    INTEGER NOT NULL DEFAULT 100,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE ideas (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  reel_id       TEXT NOT NULL REFERENCES reels(id),
  category_slug TEXT REFERENCES categories(slug),
  title         TEXT NOT NULL,
  detail        TEXT NOT NULL,
  why_relevant  TEXT NOT NULL,
  confidence    TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE evidence (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  reel_id       TEXT NOT NULL REFERENCES reels(id),
  label         TEXT NOT NULL,
  text          TEXT NOT NULL,
  at_s          REAL
);

CREATE TABLE events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            TEXT NOT NULL DEFAULT (datetime('now')),
  reel_id       TEXT,
  type          TEXT NOT NULL,
  payload       TEXT
);
CREATE INDEX idx_events_id ON events(id);

-- Starter categories, generic on purpose. Rename/replace them to fit what you save.
INSERT INTO categories (slug, name, description, status, sort_order) VALUES
  ('recipes', 'Recipes',
   'Dishes, drinks, or cooking techniques you could make yourself', 'core', 10),
  ('places', 'Places',
   'Restaurants, cafes, shops, or travel spots worth visiting', 'core', 20),
  ('learn', 'Learn',
   'Skills, tips, tools, or explanations worth learning or trying', 'core', 30),
  ('projects', 'Projects',
   'Ideas you could build, make, or start: apps, crafts, content, side projects', 'core', 40),
  ('shopping', 'Shopping',
   'Products, gear, or outfits worth considering', 'core', 50);
