import base64
import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from hamchat import db_init, db_ops as dbo
from hamchat.infra.llm.base import ChatMessage
from hamchat.media_helper import process_images
from hamchat.ui.chat_controller import ChatController, HistoryEntry


class State:
    for _name in ('_get_user_payload', '_resolve_attachment_paths', '_remove_resend_tail',
                  '_prepare_resend', 'prepare_edit_resend', 'resend_message', '_send_with_attachments'):
        locals()[_name] = getattr(ChatController, _name)

    def _save_enabled(self):
        return True

    def send_user_with_media(self, text, parts, metadata):
        mid = dbo.add_message(self._db, self._conv_id, 'user', None, text,
                              {'attachments': metadata})
        self._history.append(HistoryEntry(mid, ChatMessage('user', text, {'attachments': metadata})))
        self.outgoing = [entry.msg for entry in self._history]
        self.parts = parts

    def _on_user_text(self, text):
        mid = dbo.add_message(self._db, self._conv_id, 'user', None, text)
        self._history.append(HistoryEntry(mid, ChatMessage('user', text)))
        self.outgoing = [entry.msg for entry in self._history]


def make_state(tmp_path, monkeypatch, strict=False):
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    db_init._create_schema(db, 'open')
    user = dbo.create_user(db, name='u', handle='u', email=None, username='u', password='test', role='user')
    conv = dbo.create_conversation(db, user, 'test')
    if strict:
        # Exercise the real encrypted CAS read/write path with deterministic test crypto.
        db.execute("UPDATE meta SET value='strict' WHERE key='db_mode'")
        monkeypatch.setattr(dbo, 'encrypt_bytes_for_cas', lambda raw: (raw[::-1], b'x' * 12))
        monkeypatch.setattr(dbo, 'decrypt_bytes_for_cas', lambda ct, nonce: ct[::-1])
        monkeypatch.setattr(dbo, 'encrypt_field', lambda db, text: (text.encode(), b'x' * 12))
        monkeypatch.setattr(dbo, 'decrypt_field', lambda db, ct, nonce, *args: ct.decode())
    state = State()
    state._db, state._data_dir, state._conv_id = db, tmp_path, conv
    state._session = SimpleNamespace(current=SimpleNamespace(vision=True, role='user'))
    state._history = []
    state.errors, state.truncated = [], []
    state.chat = SimpleNamespace(attachmentRejected=SimpleNamespace(emit=state.errors.append),
                                 truncate_messages_from=state.truncated.append,
                                 append_message=lambda *a: None, draw_thumbs=lambda *a: None)
    return state


def add(state, text, images=(), role='user'):
    batch = process_images(list(images), ephemeral=False, db=state._db, data_dir=state._data_dir)
    meta = {'attachments': [dict(file_id=s['file_id'], thumb_file_id=t['file_id'], mime=s['mime'])
                            for s, t in zip(batch['stored'], batch['thumbs'])]} if images else None
    mid = dbo.add_message(state._db, state._conv_id, role, None, text, meta)
    state._history.append(HistoryEntry(mid, ChatMessage(role, text, meta)))
    return mid, meta


def image(tmp_path, name, color):
    path = tmp_path / name
    Image.new('RGB', (410, 270), color).save(path)
    return str(path)


@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('edit', [False, True])
def test_resend_originals_history_refs_and_reload(tmp_path, monkeypatch, strict, edit):
    state = make_state(tmp_path, monkeypatch, strict)
    red = image(tmp_path, 'David.png', 'red')
    blue = image(tmp_path, 'Daniel.png', 'blue')
    add(state, 'Adelaide', [red])  # two display rows, one history entry
    add(state, 'earlier answer', role='assistant')
    old, meta = add(state, 'David', [red, blue])  # starts at display row 3
    add(state, 'obsolete answer', role='assistant')
    fid = meta['attachments'][0]['file_id']
    # Make the thumbnail unmistakably different from the original.
    thumb = dbo.cas_path_for_file(state._db, meta['attachments'][0]['thumb_file_id'], data_dir=tmp_path)
    Image.new('RGB', (10, 10), 'green').save(thumb, format='PNG')
    if edit:
        payload = state.prepare_edit_resend(4)
        assert [Path(p).name for p in payload['attachments']] == ['David.png', 'Daniel.png']
        state._send_with_attachments('Daniel', [blue])
    else:
        state.resend_message(4)
    assert [m.content for m in state.outgoing] == ['Adelaide', 'earlier answer', 'Daniel' if edit else 'David']
    assert state.truncated == [3]
    assert state._db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 3
    rows = dbo.list_messages(state._db, state._conv_id)
    assert [r['content'] for r in rows] == [m.content for m in state.outgoing]
    assert len(state.parts) == (1 if edit else 2)
    img = Image.open(io.BytesIO(base64.b64decode(state.parts[0]['data_base64'])))
    assert img.size == (410, 270)
    assert img.getpixel((0, 0)) == ((0, 0, 255) if edit else (255, 0, 0))
    assert state._db.execute('SELECT ref_count FROM files WHERE id=?', (fid,)).fetchone()[0] == (1 if edit else 2)


@pytest.mark.parametrize('edit', [False, True])
def test_missing_original_preserves_everything(tmp_path, monkeypatch, edit):
    state = make_state(tmp_path, monkeypatch)
    mid, meta = add(state, '', [image(tmp_path, 'original.png', 'red')])
    dbo.cas_path_for_file(state._db, meta['attachments'][0]['file_id'], data_dir=tmp_path).unlink()
    assert (state.prepare_edit_resend(0) if edit else state.resend_message(0)) is None
    assert len(state._history) == 1
    assert state._db.execute('SELECT id FROM messages').fetchone()[0] == mid
    assert state.errors and not state.truncated


def test_text_only_ephemeral_replacement(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state._history = [HistoryEntry(None, ChatMessage('user', 'earlier')),
                      HistoryEntry(None, ChatMessage('assistant', 'answer')),
                      HistoryEntry(None, ChatMessage('user', 'old'))]
    payload = state.prepare_edit_resend(2)
    assert payload['text'] == 'old'
    state._on_user_text('new')
    assert [m.content for m in state.outgoing] == ['earlier', 'answer', 'new']


def test_image_only_reload_and_real_backend_message_builder(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    add(state, 'Adelaide', [image(tmp_path, 'earlier.png', 'yellow')])
    add(state, 'answer', role='assistant')
    add(state, '', [image(tmp_path, 'original.png', 'red')])
    add(state, 'discard this answer', role='assistant')
    # Use the real reload path, then resend the image-only bubble.
    for name in ('clear_transient_thinking', '_refresh_thinking_controls', '_refresh_ham_mem_control'):
        setattr(state, name, lambda: None)
    state.chat.insert_thumbs_after = lambda *args: None
    ChatController.load_conversation(state, state._conv_id, dbo.list_messages(state._db, state._conv_id))
    captured = []
    monkeypatch.setattr('hamchat.ui.chat_controller.make_stream_func_from_client',
                        lambda *args, **kw: lambda prompt: captured.extend(kw['build_messages'](prompt)))
    state._persist_user_with_attachments = lambda text, meta: dbo.add_message(
        state._db, state._conv_id, 'user', None, text, {'attachments': meta})
    for name in ('_begin_thinking_generation', '_activate_thinking_for_ticket',
                 '_register_thinking_generation', '_apply_model_context_allocation'):
        setattr(state, name, lambda *args: None)
    state.system_injection_if_any = lambda: None
    state._memory_snapshot = lambda flag: None
    state._memory_prefix = lambda *args: []
    state._request_thinking_modes = lambda: ('medium', 'medium')
    state._report_final_memory_snapshot = lambda *args: None
    state._use_ham_mem, state._max_turns = False, 100
    state._model_client, state._model_name = None, 'test'
    state.chat.begin_assistant_stream = lambda: 4
    state.chat.set_streaming = lambda flag: None
    state.broker = SimpleNamespace(submit=lambda stream, text: stream(text))
    state.send_user_with_media = ChatController.send_user_with_media.__get__(state)
    state.resend_message(3)
    assert [m.content for m in captured] == ['Adelaide', 'answer', '']
    assert len(captured[-1].parts) == 1
    assert captured[-1].parts[0]['width'] == 410
    ChatController.load_conversation(state, state._conv_id, dbo.list_messages(state._db, state._conv_id))
    assert [e.msg.content for e in state._history] == ['Adelaide', 'answer', '']
    assert state._get_user_payload(3)['history_index'] == 2


def test_original_retained_before_cas_cleanup(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    _, meta = add(state, 'keep image', [image(tmp_path, 'only.png', 'blue')])
    fid = meta['attachments'][0]['file_id']
    original = dbo.cas_path_for_file(state._db, fid, data_dir=tmp_path)
    delete = dbo.delete_many_messages

    def delete_and_sweep(*args, **kwargs):
        delete(*args, **kwargs)
        assert state._db.execute('SELECT ref_count FROM files WHERE id=?', (fid,)).fetchone()[0] == 0
        original.unlink()

    monkeypatch.setattr(dbo, 'delete_many_messages', delete_and_sweep)
    state.resend_message(0)
    assert original.exists()  # Normal cas_put restores the retained original.
    assert state._history[-1].msg.metadata['attachments'][0]['file_id'] == fid
    assert state._db.execute('SELECT ref_count FROM files WHERE id=?', (fid,)).fetchone()[0] == 1
    assert state.parts[0]['width'] == 410


def test_tail_delete_failure_rolls_back_references(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    mid, meta = add(state, 'original', [image(tmp_path, 'only.png', 'blue')])
    add(state, 'answer', role='assistant')
    state._db.execute("CREATE TRIGGER reject_tail BEFORE DELETE ON messages "
                      "WHEN OLD.sender_type='assistant' BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    state._db.commit()
    state.resend_message(0)
    assert len(state._history) == 2
    assert state._db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
    assert state._db.execute('SELECT COUNT(*) FROM message_files WHERE message_id=?', (mid,)).fetchone()[0] == 2
    assert not state.truncated
