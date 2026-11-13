CREATE TABLE obs_turn (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    asr_final_ms INTEGER,
    route_done_ms INTEGER,
    llm_first_token_ms INTEGER,
    tts_first_chunk_ms INTEGER,
    play_start_ms INTEGER
) STRICT;
