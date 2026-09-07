CREATE TABLE IF NOT EXISTS runs (
    id text PRIMARY KEY,
    repo text NOT NULL,
    issue integer NOT NULL CHECK (issue > 0),
    branch text NOT NULL UNIQUE,
    workspace text NOT NULL,
    artifacts text NOT NULL,
    state text NOT NULL CHECK (state IN (
        'queued', 'running', 'awaiting_approval', 'blocked', 'ready', 'closed', 'superseded'
    )),
    next_stage text NOT NULL,
    engine text NOT NULL CHECK (engine IN ('claude', 'codex')),
    auth text NOT NULL CHECK (auth IN ('subscription', 'api')),
    image text NOT NULL,
    frozen jsonb NOT NULL DEFAULT '{}',
    base_sha text,
    prepared_sha text,
    checkpoint_sha text,
    approval_sha text,
    candidate_sha text,
    approval jsonb NOT NULL DEFAULT '{}',
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    blocked_reason text,
    pr jsonb NOT NULL DEFAULT '{}',
    supersedes text REFERENCES runs(id),
    superseded_by text REFERENCES runs(id) DEFERRABLE INITIALLY DEFERRED,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- A pending supersession transfers the reservation but does not assert that
-- external PR closure has already been verified by the worker.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_unfinished_repo
    ON runs(repo)
    WHERE state IN ('queued', 'running', 'awaiting_approval', 'blocked')
      AND superseded_by IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_reserved_issue
    ON runs(repo, issue)
    WHERE state NOT IN ('closed', 'superseded') AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS runs_queue ON runs(created_at) WHERE state = 'queued';

CREATE TABLE IF NOT EXISTS attempts (
    run_id text NOT NULL REFERENCES runs(id),
    number integer NOT NULL CHECK (number BETWEEN 1 AND 3),
    stage text NOT NULL DEFAULT 'implement',
    status text NOT NULL DEFAULT 'reserved',
    candidate_sha text,
    checks jsonb NOT NULL DEFAULT '[]',
    review jsonb NOT NULL DEFAULT '{}',
    findings jsonb NOT NULL DEFAULT '[]',
    evidence jsonb NOT NULL DEFAULT '{}',
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (run_id, number)
);

CREATE TABLE IF NOT EXISTS events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs(id),
    event text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
