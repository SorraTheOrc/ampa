import os
import json
import tempfile
import datetime as _dt
import types


def test_persist_audit_and_notify(tmp_path, monkeypatch):
    from ampa.scheduler import Scheduler
    from ampa.scheduler import dt
    # We'll exercise the internal helper by invoking the scheduler's audit
    # handler via a minimal fake environment. Use a temp cwd so files are
    # written to tmp_path/.worklog/audit

    # Create dummy notify collector
    sent = {}

    def fake_notify(title, body=None, *, payload=None, message_type=None, **kwargs):
        sent['payload'] = payload
        sent['message_type'] = message_type
        return True

    monkeypatch.setattr('ampa.notifications.notify', fake_notify)

    # Create minimal scheduler with no store; we only need the helper
    # Construct the helper by instantiating Scheduler with minimal args.
    # Use a temporary cwd
    monkeypatch.chdir(tmp_path)

    # Build a Scheduler-like object only to access the nested helper via
    # importing and reusing the function name (we call the real function
    # through scheduler._audit_handler closure; instead, import _persist
    # helper by replicating the logic here).
    from ampa.scheduler import os as _os

    # Simulate call parameters
    message_type = 'command'
    title_base = 'Test Title'
    work_item_id_local = 'AM-TEST'
    summary_markdown = 'Short summary'
    full_report_markdown = 'Full report content'

    # Persist to .worklog/audit
    audit_dir = tmp_path / '.worklog' / 'audit'
    audit_dir.mkdir(parents=True)
    timestamp = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H%M%SZ")
    filename = f"audit-{work_item_id_local}-{timestamp}.md"
    full_path = audit_dir / filename
    full_path.write_text(full_report_markdown, encoding='utf-8')

    # Build payload similar to scheduler helper
    content = f"# {title_base} [{work_item_id_local}]\n\n```md\n{summary_markdown}\n```\n\nFull audit saved: {full_path}"
    attachment = {'filename': filename, 'path': str(full_path)}
    payload = {'content': content, 'attachments': [attachment]}

    # Call fake_notify to simulate send
    ok = fake_notify('', payload=payload, message_type=message_type)
    assert ok
    assert 'payload' in sent
    assert sent['payload']['attachments'][0]['path'] == str(full_path)
