CREATE TABLE users (
    id bigint PRIMARY KEY,
    quality text NOT NULL DEFAULT 'original',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE admins (user_id bigint PRIMARY KEY);
CREATE TABLE bot_config (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id),
    url text NOT NULL,
    platform text NOT NULL,
    quality text NOT NULL,
    state text NOT NULL CHECK (state IN ('choice','queued','running','delivering','done','failed','cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    available_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    lease uuid,
    lease_until timestamptz,
    message_id bigint,
    error text,
    cache_files jsonb
);
CREATE INDEX job_queue ON jobs (available_at, created_at) WHERE state = 'queued';
CREATE INDEX job_leases ON jobs (lease_until) WHERE state IN ('running','delivering');
CREATE INDEX job_history ON jobs (user_id, created_at DESC);
CREATE TABLE system_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid,
    event text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX system_events_age ON system_events (created_at);
CREATE TABLE platform_stats (
    platform text NOT NULL,
    day date NOT NULL DEFAULT CURRENT_DATE,
    state text NOT NULL,
    total bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, day, state)
);
