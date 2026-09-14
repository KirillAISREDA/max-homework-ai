"""Приглашения «ребёнок ↔ родитель»: ссылка и запасной код (спецификация онбординга §4.4, §7)."""

import pytest

from hwcheck.bot.invites import (
    CODE_ALPHABET,
    CODE_LENGTH,
    MAX_START_PAYLOAD,
    InviteKind,
    digest,
    new_invite,
    parse_code,
    parse_start_payload,
    start_payload,
)


def test_new_invite_has_url_safe_token_and_readable_code() -> None:
    first, second = new_invite("student_invites_parent"), new_invite("student_invites_parent")
    assert first.token != second.token and first.code != second.code
    assert len(first.code) == CODE_LENGTH and set(first.code) <= set(CODE_ALPHABET)
    assert first.display_code == f"{first.code[:4]}-{first.code[4:]}"


def test_link_carries_kind_prefix_within_max_limit() -> None:
    invite = new_invite("student_invites_parent")
    link = invite.link("domashka_bot")
    assert link == f"https://max.ru/domashka_bot?start=p_{invite.token}"
    assert len(start_payload(invite.kind, invite.token)) <= MAX_START_PAYLOAD
    child = new_invite("parent_invites_student")
    assert child.link("domashka_bot").endswith(f"?start=c_{child.token}")


def test_start_payload_roundtrip() -> None:
    kinds: tuple[InviteKind, ...] = ("student_invites_parent", "parent_invites_student")
    for kind in kinds:
        invite = new_invite(kind)
        assert parse_start_payload(start_payload(invite.kind, invite.token)) == (kind, invite.token)


@pytest.mark.parametrize(
    "payload", [None, "", "p_", "x_abcdefghijklmnopqrstu", "p_short", "p_bad token!"]
)
def test_broken_start_payload_is_ignored(payload: str | None) -> None:
    assert parse_start_payload(payload) is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("4F7K-92QD", "4F7K92QD"),
        (" 4f7k92qd ", "4F7K92QD"),
        ("4F7К-92QD", "4F7K92QD"),  # «К» кириллицей — ребёнок набирает в русской раскладке
    ],
)
def test_code_is_parsed_leniently(text: str, code: str) -> None:
    assert parse_code(text) == code


@pytest.mark.parametrize("text", ["4F7K-92Q0", "привет", "4F7K-92QDX", "1234 5678"])
def test_not_a_code(text: str) -> None:
    assert parse_code(text) is None


def test_hashes_do_not_reveal_secrets() -> None:
    invite = new_invite("parent_invites_student")
    assert invite.token_hash == digest(invite.token) and len(invite.token_hash) == 64
    assert invite.code not in invite.code_hash and invite.token not in invite.token_hash
