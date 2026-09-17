-- База знаний по предметам и находки проверки (спецификация каркаса 2026-09-16, §5, §8).
-- В базе — только печатный текст учебника и наши ответы; фото тетрадей и персональных данных нет.

CREATE TABLE kb_pages (
  id            bigserial PRIMARY KEY,
  subject       text NOT NULL,
  grade         smallint CHECK (grade BETWEEN 1 AND 9),
  fingerprint   text NOT NULL,
  text          text NOT NULL,
  photo_path    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (subject, fingerprint)
);

CREATE TABLE kb_tasks (
  id            bigserial PRIMARY KEY,
  page_id       bigint NOT NULL REFERENCES kb_pages ON DELETE CASCADE,
  number        text,
  condition     text NOT NULL,
  task_kind     text NOT NULL
);
CREATE INDEX kb_tasks_page ON kb_tasks (page_id);

CREATE TABLE kb_answers (
  id            bigserial PRIMARY KEY,
  task_id       bigint NOT NULL REFERENCES kb_tasks ON DELETE CASCADE,
  answer        jsonb NOT NULL,
  derived_by    text NOT NULL,
  checked_by    text,
  status        text NOT NULL DEFAULT 'unverified'
                CHECK (status IN ('unverified', 'verified', 'rejected')),
  reviewed_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX kb_answers_task ON kb_answers (task_id);
CREATE INDEX kb_answers_review ON kb_answers (status) WHERE status = 'unverified';

CREATE TABLE kb_rules (
  code          text PRIMARY KEY,
  subject       text NOT NULL,
  grade_from    smallint NOT NULL CHECK (grade_from BETWEEN 1 AND 9),
  title         text NOT NULL,
  statement     text NOT NULL,
  example       text NOT NULL,
  finding_kinds text[] NOT NULL
);

CREATE TABLE kb_words (
  subject       text NOT NULL,
  word          text NOT NULL CHECK (char_length(word) <= 64),  -- слово, а не абзац текста
  source        text NOT NULL,
  attrs         jsonb,
  PRIMARY KEY (subject, word, source)
);
CREATE INDEX kb_words_source ON kb_words (subject, source);

-- находки проверки: основа аналитики качества и модели ученика; homework_id — этап 4 онбординга
CREATE TABLE findings (
  id            bigserial PRIMARY KEY,
  user_hash     text NOT NULL,
  subject       text NOT NULL,
  trace_id      text,
  task_number   text,
  kind          text NOT NULL,
  strength      text NOT NULL CHECK (strength IN ('verified', 'candidate', 'feedback')),
  rule_code     text,
  confirmed     boolean,
  resolved      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX findings_user_time ON findings (user_hash, created_at);
