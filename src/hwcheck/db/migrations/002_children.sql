-- Онбординг, этап 2 (спецификация §4.7, §6; решение 16.09): дети 1–4 классов пользуются Домашкой через
-- аккаунт родителя, у родителя их может быть несколько — профиль ребёнка больше не совпадает с аккаунтом MAX.
-- Таблицы этапа 1 пусты (в них ещё не писал ни один код) и пересоздаются; если данные всё же есть —
-- миграция падает, а не теряет их.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM student_profiles) OR EXISTS (SELECT 1 FROM consents)
     OR EXISTS (SELECT 1 FROM homeworks) THEN
    RAISE EXCEPTION '002_children: в student_profiles, consents или homeworks есть данные';
  END IF;
END $$;

DROP TABLE homeworks;
DROP TABLE consents;
DROP TABLE student_profiles;

CREATE TABLE student_profiles (
  id               bigserial PRIMARY KEY,
  user_id          bigint UNIQUE REFERENCES users ON DELETE CASCADE,  -- свой MAX (5–9 класс)
  parent_user_id   bigint REFERENCES users ON DELETE SET NULL,
  grade            smallint NOT NULL CHECK (grade BETWEEN 1 AND 9),
  grade_year       smallint NOT NULL,
  grade_asked_year smallint NOT NULL,
  subject          text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  -- фото присылает свой аккаунт ребёнка или родитель; удаление родителя раньше детей 1–4 упрётся сюда,
  -- а не оставит профиль без владельца
  CHECK (user_id IS NOT NULL OR parent_user_id IS NOT NULL),
  CHECK (user_id IS DISTINCT FROM parent_user_id)
);
CREATE INDEX student_profiles_parent ON student_profiles (parent_user_id);

-- юридическая запись: без FK, переживает удаление данных
CREATE TABLE consents (
  id                 bigserial PRIMARY KEY,
  parent_hash        text NOT NULL,
  student_profile_id bigint NOT NULL,
  student_hash       text,            -- хэш MAX ребёнка; NULL — фото присылает родитель
  policy_version     text NOT NULL,
  given_at           timestamptz NOT NULL,
  revoked_at         timestamptz
);
-- у ребёнка один подтвердивший родитель
CREATE UNIQUE INDEX consents_one_active ON consents (student_profile_id) WHERE revoked_at IS NULL;

CREATE TABLE homeworks (
  id               bigserial PRIMARY KEY,
  student_id       bigint NOT NULL REFERENCES student_profiles ON DELETE CASCADE,
  subject          text NOT NULL,
  tasks_total      smallint NOT NULL,
  tasks_correct    smallint NOT NULL,
  tasks_wrong      smallint NOT NULL,
  tasks_uncertain  smallint NOT NULL,
  errors_resolved  smallint NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX homeworks_student_time ON homeworks (student_id, created_at);

-- класс из ссылки родителя: ребёнок по ней не выбирает класс заново
ALTER TABLE invites ADD COLUMN grade smallint CHECK (grade BETWEEN 1 AND 9);
ALTER TABLE invites ADD CONSTRAINT invites_parent_invite_complete CHECK (
  kind <> 'parent_invites_student'
  OR (grade IS NOT NULL AND consent_given_at IS NOT NULL AND policy_version IS NOT NULL)
);
