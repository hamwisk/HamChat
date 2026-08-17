import sqlite3

from hamchat import db_ops


def _conversation_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE saved_conversations ("
        "id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, created INTEGER)"
    )
    return conn


def test_conversation_summary_pages_expose_chats_older_than_initial_50():
    conn = _conversation_db()
    conn.executemany(
        "INSERT INTO saved_conversations(id, user_id, title, created) VALUES (?, 1, ?, ?)",
        [(number, f"Chat {number}", number) for number in range(1, 124)],
    )

    first = db_ops.list_conversations(conn, user_id=1)
    second = db_ops.list_conversations(conn, user_id=1, offset=len(first))
    third = db_ops.list_conversations(conn, user_id=1, offset=len(first) + len(second))

    assert [len(first), len(second), len(third)] == [50, 50, 23]
    ids = [row["id"] for page in (first, second, third) for row in page]
    assert ids == list(range(123, 0, -1))
    assert len(ids) == len(set(ids))
    assert db_ops.list_conversations(conn, user_id=1, offset=len(ids)) == []


def test_conversation_search_finds_an_older_chat_not_in_initial_page():
    conn = _conversation_db()
    conn.executemany(
        "INSERT INTO saved_conversations(id, user_id, title, created) VALUES (?, 1, ?, ?)",
        [(number, f"Chat {number}", number) for number in range(1, 75)],
    )
    conn.execute(
        "INSERT INTO saved_conversations(id, user_id, title, created) VALUES (75, 2, 'Needle', 75)"
    )
    conn.execute(
        "UPDATE saved_conversations SET title='Needle in an older chat' WHERE id=1"
    )

    initial_ids = {row["id"] for row in db_ops.list_conversations(conn, user_id=1)}
    matches = db_ops.list_conversations(conn, user_id=1, search="needle", limit=None)

    assert 1 not in initial_ids
    assert [(row["id"], row["title"]) for row in matches] == [(1, "Needle in an older chat")]
