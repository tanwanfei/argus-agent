CREATE TABLE doc_chunk (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL,
    label TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (source, doc_id, ordinal)
) STRICT;
CREATE VIRTUAL TABLE doc_chunk_vec USING vec0(embedding float[512] distance_metric=cosine);
