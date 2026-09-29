-- Отчёт родителю раз в неделю (решение 29.09): выключатель и отметка недели.
-- Отметка ставится ДО отправки: после рестарта отчёт не уходит второй раз.
ALTER TABLE parent_settings ADD COLUMN weekly_report boolean NOT NULL DEFAULT true;

CREATE TABLE weekly_reports (
  parent_user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  period_end     timestamptz NOT NULL,   -- слот (вс 18:00 МСК) в UTC
  status         text NOT NULL DEFAULT 'sending'
                 CHECK (status IN ('sending', 'sent', 'failed', 'skipped')),
  claimed_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  PRIMARY KEY (parent_user_id, period_end)
);
