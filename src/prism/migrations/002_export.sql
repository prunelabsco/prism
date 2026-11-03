-- Fase 3: tag asli mymind (salinan pengaman di catatan Nexus) + hash file hasil export
ALTER TABLE reels ADD COLUMN source_tags TEXT;  -- JSON list, NULL = belum pernah dicatat
ALTER TABLE reels ADD COLUMN export_hash TEXT;  -- sha256 file saat terakhir ditulis mesin
