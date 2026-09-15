-- Онбординг, согласие родителя, профиль ученика (спецификация 2026-09-14, §6).
-- Сырой id MAX не хранится: max_user_hash — HMAC (events.anonymize), max_user_id_enc — Fernet.

CREATE TABLE users (
  id               bigserial PRIMARY KEY,
  max_user_hash    text NOT NULL UNIQUE,
  max_user_id_enc  bytea NOT NULL,
  role             text NOT NULL CHECK (role IN ('student', 'parent')),
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE student_profiles (
  user_id          bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  grade            smallint CHECK (grade BETWEEN 1 AND 9),
  grade_year       smallint,
  grade_asked_year smallint,
  subject          text,
  parent_user_id   bigint REFERENCES users ON DELETE SET NULL,
  CHECK (parent_user_id IS DISTINCT FROM user_id)
);

CREATE TABLE parent_settings (
  user_id          bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  notify_mode      text NOT NULL DEFAULT 'digest' CHECK (notify_mode IN ('instant', 'digest', 'off')),
  digest_time      time NOT NULL DEFAULT '20:00',
  utc_offset_min   smallint NOT NULL DEFAULT 180,
  last_digest_at   timestamptz
);

-- юридическая запись: без FK, переживает удаление данных
CREATE TABLE consents (
  id               bigserial PRIMARY KEY,
  parent_hash      text NOT NULL,
  student_hash     text NOT NULL,
  policy_version   text NOT NULL,
  given_at         timestamptz NOT NULL,
  revoked_at       timestamptz
);
CREATE UNIQUE INDEX consents_one_active_parent ON consents (student_hash) WHERE revoked_at IS NULL;

-- в базе только sha256 токена ссылки и запасного кода
CREATE TABLE invites (
  token_hash       text PRIMARY KEY,
  code_hash        text NOT NULL UNIQUE,
  kind             text NOT NULL CHECK (kind IN ('student_invites_parent', 'parent_invites_student')),
  created_by       bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  consent_given_at timestamptz,
  policy_version   text,
  expires_at       timestamptz NOT NULL,
  used_at          timestamptz,
  used_by          bigint REFERENCES users ON DELETE SET NULL
);

CREATE TABLE homeworks (
  id               bigserial PRIMARY KEY,
  student_user_id  bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  subject          text NOT NULL,
  tasks_total      smallint NOT NULL,
  tasks_correct    smallint NOT NULL,
  tasks_wrong      smallint NOT NULL,
  tasks_uncertain  smallint NOT NULL,
  errors_resolved  smallint NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX homeworks_student_time ON homeworks (student_user_id, created_at);

CREATE TABLE subject_waitlist (
  user_hash        text NOT NULL,
  subject          text NOT NULL,
  grade            smallint NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_hash, subject)
);

-- лимит ввода запасного кода
CREATE TABLE login_attempts (
  user_hash        text NOT NULL,
  attempted_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_attempts_user_time ON login_attempts (user_hash, attempted_at);
