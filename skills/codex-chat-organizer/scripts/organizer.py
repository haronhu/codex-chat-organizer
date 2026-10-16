#!/usr/bin/env python3
"""Local safety/state engine; never writes Codex databases or calls a model."""
import argparse
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager

CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'categories.json').read_text())
TERMINAL = {'idle', 'completed', 'failed', 'interrupted'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def compact(text):
    return re.sub(r'\s+', ' ', text).strip()


def safe_text(text):
    # Context/instructions and credentials must not become classification evidence or titles.
    text = text.replace('\\n', '\n')
    for tag in ('rules', 'environment_context', 'system_reminder', 'INSTRUCTIONS',
                'skills_instructions', 'permissions', 'permissions instructions',
                'in-app-browser-context', 'response-annotations'):
        text = re.sub(r'<'+re.escape(tag)+r'\b[^>]*>.*?</'+re.escape(tag)+r'>', '', text, flags=re.S|re.I)
    query = re.search(r'<user_query>(.*?)</user_query>', text, re.S)
    if query:
        text = query.group(1)
    request = re.search(r'## My request(?: for Codex)?:\s*(.*)', text, re.S)
    if request:
        text = request.group(1)
    text = re.sub(r'(?i)\b(?:gh[pousr]_[\w]+|sk-[\w-]{12,})', '[REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_ -]?key|password|token|密码|密钥|apikey)\s*[=:：是]\s*)[^\s,，;；]+', r'\1[REDACTED]', text)
    text = re.sub(r'https?://\S+', '[链接]', text)
    return compact(text)


def generated_title(title, first):
    title = compact(re.sub(r'[`*_]', '', title)).rstrip('…').rstrip('.')
    first = compact(re.sub(r'[`*_]', '', safe_text(first)))
    return not title or (len(title) >= 8 and first.startswith(title))


def meaningful_change(old, new):
    if not old:
        return True
    if digest(old) == digest(new):
        return False
    # Compare intent-bearing user turns, not tool logs, timestamps or assistant chatter.
    a, b = '\n'.join(old), '\n'.join(new)
    ratio = 1 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    novel = sum(len(x) for x in new if x not in old)
    return ratio >= CONFIG['meaningful_change_ratio'] or novel >= CONFIG['meaningful_change_characters']


def write_json(path, value):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_json(path):
    return json.loads(Path(path).expanduser().read_text())


def ro_database(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError('Missing Codex database: '+str(path))
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


class LocalSource:
    """Version-checked, read-only adapter for local Codex conversations only."""
    def __init__(self, home):
        home = Path(home).expanduser()
        paths = sorted(home.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]), reverse=True)
        if not paths:
            raise ValueError('No local Codex state database')
        self.db = ro_database(paths[0])
        cols = {r['name'] for r in self.db.execute('PRAGMA table_info(threads)')}
        required = {'id','title','name','rollout_path','first_user_message','archived','source','thread_source','updated_at'}
        if not required <= cols:
            raise ValueError('Unsupported Codex state schema; stop rather than guess')
        self.history = ro_database(home / 'thread_history_1.sqlite')
        for table, required in {
            'thread_turns': {'thread_id','rollout_ordinal','status'},
            'thread_items': {'thread_id','rollout_ordinal','item_type','item_json'},
        }.items():
            cols = {r['name'] for r in self.history.execute('PRAGMA table_info('+table+')')}
            if not required <= cols:
                raise ValueError('Unsupported Codex history schema: '+table)

    def close(self):
        self.db.close()
        self.history.close()

    def inventory(self, include_archived=False):
        rows = self.db.execute("SELECT id,COALESCE(NULLIF(name,''),title) AS title,first_user_message,updated_at,archived,source,thread_source FROM threads WHERE source IN ('cli','vscode') AND thread_source='user'" + ('' if include_archived else ' AND archived=0') + ' ORDER BY updated_at DESC,id').fetchall()
        result = []
        for row in rows:
            item = dict(row)
            turn = self.history.execute('SELECT status,rollout_ordinal FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1', (row['id'],)).fetchone()
            item['status'] = turn['status'] if turn else 'unknown'
            item['watermark'] = self.history.execute('SELECT COALESCE(MAX(rollout_ordinal),0) FROM thread_items WHERE thread_id=?', (row['id'],)).fetchone()[0]
            item['hostId'] = 'local'
            item['kind'] = 'codex'
            result.append(item)
        return result

    def get(self, thread_id):
        return next((x for x in self.inventory(True) if x['id'] == thread_id), None)

    def evidence(self, thread):
        rows = self.history.execute("SELECT item_json,item_type FROM thread_items WHERE thread_id=? AND item_type='userMessage' ORDER BY rollout_ordinal DESC LIMIT 4", (thread['id'],)).fetchall()
        rows = list(reversed(rows)) + list(self.history.execute("SELECT item_json,item_type FROM thread_items WHERE thread_id=? AND item_type='agentMessage' ORDER BY rollout_ordinal DESC LIMIT 8", (thread['id'],)).fetchall())
        users, finals = [], []
        for row in rows:
            item = json.loads(row['item_json'])
            if row['item_type'] == 'userMessage':
                text = safe_text('\n'.join(x.get('text','') for x in item.get('content',[]) if x.get('type') == 'text'))
                if text:
                    users.append(text[:1600])
            elif item.get('phase') in ('final_answer','final'):
                text = safe_text(item.get('text',''))
                if text:
                    finals.append(text[:1200])
        if not users:
            raise ValueError('No readable user messages in history projection; requires read_thread')
        first = safe_text(thread['first_user_message'])[:1600]
        intents = ([first] if first and first != users[0] else []) + users[-4:]
        return {'user_intents': intents, 'recent_outcomes': list(reversed(finals[:2]))}


class Store:
    def __init__(self, path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS observed (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal (
            id TEXT PRIMARY KEY, thread_key TEXT NOT NULL, state TEXT NOT NULL, value TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS single_pending ON journal(thread_key) WHERE state='pending';
        CREATE TABLE IF NOT EXISTS notices (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def get(self, key):
        row = self.db.execute('SELECT value FROM observed WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else {}

    def save(self, key, value):
        self.db.execute('INSERT INTO observed VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value,ensure_ascii=False)))

    def pending(self, key=None):
        sql = "SELECT value FROM journal WHERE state='pending'"
        return [json.loads(x['value']) for x in self.db.execute(sql+(' AND thread_key=?' if key else ''), (key,) if key else ())]

    def notice(self, key, value):
        fingerprint = digest(value)
        old = self.db.execute('SELECT fingerprint FROM notices WHERE key=?',(key,)).fetchone()
        if old and old['fingerprint'] == fingerprint:
            return False
        self.db.execute('INSERT INTO notices VALUES(?,?) ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint',(key,fingerprint))
        return True


def thread_key(thread):
    return thread.get('kind','codex')+':'+thread.get('hostId','local')+':'+thread['id']


def scan(source, store, full=False, include_archived=False, exclude=(), live_inventory=None):
    candidates, skipped, errors, ready = [], [], [], []
    threads = source.inventory(include_archived)
    live = {}
    if live_inventory is not None:
        checked = live_inventory.get('checked_at',0)
        if not isinstance(checked,(int,float)) or not math.isfinite(checked) or not 0 <= time.time()-checked <= 60:
            raise ValueError('Application inventory older than 60 seconds; refresh list_threads')
        live = {t['id']:t for t in live_inventory.get('threads',[]) if t.get('kind')=='codex' and t.get('hostId')=='local'}
    with store.transaction():
        for t in threads:
            app = live.get(t['id'])
            if app and app.get('status') not in ('idle','notLoaded'):
                t['status'] = app.get('status','unknown')
            key = thread_key(t)
            old = store.get(key)
            reason = None
            if t['id'] in exclude:
                reason = 'excluded'
            elif t['status'] not in TERMINAL:
                reason = 'running_or_unknown'
            elif store.pending(key):
                reason = 'pending_operation'
            elif old.get('protected'):
                reason = 'protected_title'
            elif old.get('managed_title') and t['title'] != old['managed_title']:
                old.update(protected=True, protection_reason='external_title_change')
                store.save(key, old)
                reason = 'external_title_change'
            elif old.get('observed_title') and t['title'] != old['observed_title']:
                old.update(protected=True, protection_reason='external_title_change')
                store.save(key, old)
                reason = 'external_title_change'
            elif not full and old.get('watermark') == t['watermark'] and not old.get('retry'):
                reason = 'unchanged_metadata'
            if reason:
                skipped.append({'id':t['id'],'title':t['title'],'reason':reason})
                if reason == 'unchanged_metadata' and old.get('approved_action'):
                    ready.append(old['approved_action'])
                continue
            try:
                evidence = source.evidence(t)
            except (ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
                errors.append({'id':t['id'],'error':str(exc)})
                continue
            changed = meaningful_change(old.get('classified_intents',[]),evidence['user_intents'])
            needs_review = not old and not generated_title(t['title'],t['first_user_message'])
            old.update(observed_title=t['title'],watermark=t['watermark'],last_seen=time.time())
            if changed or full or old.get('retry'):
                candidate = {**{k:t[k] for k in ('id','title','kind','hostId','status','watermark','archived')},
                             'evidence':evidence,'evidence_hash':digest(evidence),
                             'requires_live_preflight':True,
                             'initial_custom_title':needs_review or old.get('initial_custom_title',False)}
                candidates.append(candidate)
                old.pop('approved_action',None)
                old.update(candidate=candidate,initial_custom_title=candidate['initial_custom_title'],retry=True)
            else:
                skipped.append({'id':t['id'],'title':t['title'],'reason':'no_meaningful_change'})
                if old.get('approved_action'):
                    # Intent stayed stable; refresh the optimistic preflight watermark.
                    action = old['approved_action']
                    action['watermark'] = t['watermark']
                    action.pop('action_hash',None)
                    action['action_hash'] = digest(action)
                    ready.append(old['approved_action'])
            store.save(key,old)
    return {'version':1,'mode':'full' if full else 'incremental','scope':'local_codex_user_chats',
            'include_archived':include_archived,'inventory_count':len(threads),
            'candidates':candidates,'ready_actions':ready,'skipped':skipped,'errors':errors,'pending':store.pending()}


def make_plan(store, decisions):
    result = {'version':1,'actions':[],'suggestions':[],'unchanged':[]}
    seen = set()
    with store.transaction():
        for decision in decisions:
            key = thread_key(decision)
            if key in seen:
                raise ValueError('Duplicate decision: '+key)
            seen.add(key)
            old = store.get(key)
            candidate = old.get('candidate')
            if not candidate or decision.get('evidence_hash') != candidate['evidence_hash']:
                raise ValueError('Unknown or stale candidate: '+key)
            if old.get('protected') or store.pending(key):
                raise ValueError('Protected or pending conversation: '+key)
            title = compact(decision.get('title',''))
            category = decision.get('category')
            confidence = decision.get('confidence',0)
            if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError('Invalid confidence')
            reason = None
            if category not in CONFIG['categories']:
                reason = 'unknown_category'
            elif decision.get('multi_topic') is not False:
                reason = 'multi_topic_or_unspecified'
            elif confidence < CONFIG['minimum_confidence']:
                reason = 'low_confidence'
            elif candidate['initial_custom_title']:
                reason = 'existing_custom_title_needs_confirmation'
            elif not title.startswith('['+category+'] ') or not title.split('] ',1)[-1]:
                reason = 'invalid_title_format'
            elif len(title) > CONFIG['max_title_characters']:
                reason = 'title_too_long'
            elif safe_text(title) != title or '[REDACTED]' in title:
                reason = 'unsafe_title'
            action = {'id':candidate['id'],'kind':candidate['kind'],'hostId':candidate['hostId'],
                      'old_title':candidate['title'],'new_title':title,'evidence_hash':candidate['evidence_hash'],
                      'watermark':candidate['watermark'],'category':category,'confidence':confidence,
                      'reason':decision.get('reason',''),'operation':'rename'}
            old.pop('approved_action',None)
            if reason:
                action['blocked_reason'] = reason
                result['suggestions'].append(action)
            elif action['old_title'] == title:
                result['unchanged'].append(action)
            else:
                action['action_hash'] = digest(action)
                old['approved_action'] = action
                result['actions'].append(action)
            old.update(classified_intents=candidate['evidence']['user_intents'],retry=False)
            store.save(key,old)
    return result


def require_live(live, action, expected_title):
    if live.get('id') != action['id'] or live.get('title') != expected_title:
        raise ValueError('Fresh application title does not match; do not write')
    if live.get('status') not in TERMINAL:
        raise ValueError('Conversation active, awaiting input, or unknown; do not write')
    if live.get('hostId','local') != 'local' or live.get('kind','codex') != 'codex':
        raise ValueError('V1 supports local Codex conversations only')
    checked = live.get('checked_at')
    if isinstance(checked,bool) or not isinstance(checked,(int,float)) or not math.isfinite(checked) or not 0 <= time.time()-checked <= 30:
        raise ValueError('Live check missing or older than 30 seconds')


def begin(source, store, action, live):
    key = thread_key(action)
    require_live(live,action,action['old_title'])
    with store.transaction():
        old = store.get(key)
        expected = old.get('approved_action')
        if old.get('protected') or not expected or expected != action:
            raise ValueError('Action not approved or conversation protected')
        if store.pending(key):
            raise ValueError('Pending operation must be reconciled first')
        current = source.get(action['id'])
        if not current or current['title'] != action['old_title'] or current['status'] not in TERMINAL:
            raise ValueError('Local title/status changed; do not write')
        if action['operation'] == 'rename' and current['watermark'] != action['watermark']:
            raise ValueError('Conversation content changed after preview; rescan')
        record = {**action,'operation_id':uuid.uuid4().hex,'state':'pending','started_at':time.time()}
        store.db.execute('INSERT INTO journal VALUES(?,?,?,?)',(record['operation_id'],key,'pending',json.dumps(record,ensure_ascii=False)))
        return record


def approve_title(store, thread_id, title):
    """Only call after an explicit user confirmation; protect on success."""
    title = compact(title)
    if not title or len(title) > CONFIG['max_title_characters'] or safe_text(title) != title or '[REDACTED]' in title:
        raise ValueError('Invalid confirmed title')
    key = 'codex:local:'+thread_id
    with store.transaction():
        old = store.get(key)
        candidate = old.get('candidate')
        if not candidate or old.get('protected') or store.pending(key):
            raise ValueError('Missing candidate, protected title, or pending operation')
        action = {'id':thread_id,'kind':'codex','hostId':'local','old_title':candidate['title'],
                  'new_title':title,'watermark':candidate['watermark'],'operation':'rename',
                  'evidence_hash':candidate['evidence_hash'],'protect_after':True,
                  'reason':'Explicitly confirmed title'}
        action['action_hash'] = digest(action)
        old['approved_action'] = action
        old.update(classified_intents=candidate['evidence']['user_intents'],retry=False)
        store.save(key,old)
        return action


def finish(source, store, operation_id, outcome, live=None):
    with store.transaction():
        row = store.db.execute('SELECT * FROM journal WHERE id=?',(operation_id,)).fetchone()
        if not row or row['state'] != 'pending':
            raise ValueError('Unknown or already finalized operation')
        record = json.loads(row['value'])
        current = source.get(record['id'])
        if not current:
            raise ValueError('Conversation unavailable; leave pending')
        if outcome == 'success':
            require_live(live or {},record,record['new_title'])
            if current['title'] != record['new_title']:
                raise ValueError('Rename not visible in local index; leave pending and retry verification')
        elif outcome == 'failed':
            if current['title'] != record['old_title']:
                raise ValueError('Failure uncertain: title changed; leave pending and reconcile')
        else:
            raise ValueError('Outcome must be success or failed')
        old = store.get(row['thread_key'])
        if outcome == 'success':
            old.update(managed_title=record['new_title'],observed_title=record['new_title'],retry=False)
            if record['operation'] == 'restore':
                old.update(protected=True,protection_reason='restored_title')
            elif record.get('protect_after'):
                old.update(protected=True,protection_reason='manual_confirmation')
        else:
            old['retry'] = True
        old.pop('approved_action',None)
        store.save(row['thread_key'],old)
        record.update(state=outcome,finished_at=time.time())
        store.db.execute('UPDATE journal SET state=?,value=? WHERE id=?',(outcome,json.dumps(record,ensure_ascii=False),operation_id))
        return record


def restore_plan(source, store, operation_id):
    row = store.db.execute('SELECT value,state FROM journal WHERE id=?',(operation_id,)).fetchone()
    if not row or row['state'] != 'success':
        raise ValueError('Only successful recorded operations can be restored')
    record = json.loads(row['value'])
    if record['operation'] != 'rename':
        raise ValueError('Cannot restore a restoration')
    current = source.get(record['id'])
    if not current or current['title'] != record['new_title']:
        raise ValueError('Title changed after rename; restore would overwrite user changes')
    key = thread_key(record)
    with store.transaction():
        old = store.get(key)
        if old.get('protected') or store.pending(key):
            raise ValueError('Protected or pending conversation')
        action = {'id':record['id'],'kind':record['kind'],'hostId':record['hostId'],
                  'old_title':record['new_title'],'new_title':record['old_title'],
                  'operation':'restore','restore_of':operation_id,'watermark':current['watermark']}
        action['action_hash'] = digest(action)
        old['approved_action'] = action
        store.save(key,old)
        return action


def notifications(store, report):
    alerts = []
    with store.transaction():
        for group in ('suggestions','errors','pending'):
            for item in report.get(group,[]):
                key = group+':'+item.get('id',item.get('operation_id','unknown'))
                # Time fields must not retrigger identical notifications.
                stable = {k:v for k,v in item.items() if k not in ('started_at','finished_at','checked_at','watermark','evidence_hash','action_hash')}
                if store.notice(key,stable):
                    alerts.append({'type':group,'item':item})
    return {'notify':bool(alerts),'alerts':alerts}


def default_home():
    return Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--codex-home',default=str(default_home()))
    p.add_argument('--state',default=str(default_home()/'chat-organizer/state.sqlite'))
    sub = p.add_subparsers(dest='command',required=True)
    s = sub.add_parser('scan',help='Read-only full/incremental discovery; never renames')
    s.add_argument('--full',action='store_true')
    s.add_argument('--include-archived',action='store_true')
    s.add_argument('--exclude',action='append',default=[])
    s.add_argument('--live-inventory',help='Optional fresh application list_threads metadata')
    s.add_argument('--output',required=True)
    s = sub.add_parser('plan')
    s.add_argument('--decisions',required=True)
    s.add_argument('--output',required=True)
    s = sub.add_parser('begin',help='Persist rollback intent before calling set_thread_title')
    s.add_argument('--action',required=True)
    s.add_argument('--live',required=True)
    s = sub.add_parser('finish')
    s.add_argument('--operation-id',required=True)
    s.add_argument('--outcome',choices=['success','failed'],required=True)
    s.add_argument('--live')
    s = sub.add_parser('protect',help='Mark a manually confirmed title as immutable')
    s.add_argument('--id',required=True)
    s = sub.add_parser('approve',help='Explicit user-confirmed title; still requires begin/tool/finish')
    s.add_argument('--id',required=True)
    s.add_argument('--title',required=True)
    s.add_argument('--output',required=True)
    s = sub.add_parser('restore-plan')
    s.add_argument('--operation-id',required=True)
    s.add_argument('--output',required=True)
    s = sub.add_parser('history')
    s = sub.add_parser('notify')
    s.add_argument('--report',required=True)
    s = sub.add_parser('schedule-prompt',help='Print prompt; does not install or schedule anything')
    s.add_argument('--apply',action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    if args.command == 'schedule-prompt':
        skill = Path(__file__).resolve().parents[1]/'SKILL.md'
        mode = '执行模式：我授权按安全计划自动改名；不是仅预览。' if args.apply else '只做预览，禁止实际改名。'
        print(f'使用 $codex-chat-organizer（先读取 {skill}）。{mode}首次全量盘点本机未归档用户会话，之后增量处理。运行中的会话和本会话跳过；已确认或手动修改的标题保护；不确定和多主题只给建议。所有改名先记账再调用 Codex 应用工具，并验证结果；异常停写。无变化或无需要确认的问题时保持安静，仅对新的异常和建议提醒。禁止上传本地会话、运行数据和凭证，禁止改写 Codex 内部数据库。')
        return 0
    source = None
    store = Store(args.state)
    try:
        if args.command in ('scan','begin','finish','restore-plan'):
            source = LocalSource(args.codex_home)
        if args.command == 'scan':
            result = scan(source,store,args.full,args.include_archived,args.exclude,load_json(args.live_inventory) if args.live_inventory else None)
            write_json(args.output,result)
            print(json.dumps({k:len(result[k]) for k in ('candidates','ready_actions','skipped','errors','pending')}))
        elif args.command == 'plan':
            result = make_plan(store,load_json(args.decisions))
            write_json(args.output,result)
            print(json.dumps({k:len(result[k]) for k in ('actions','suggestions','unchanged')}))
        elif args.command == 'begin':
            print(json.dumps(begin(source,store,load_json(args.action),load_json(args.live)),ensure_ascii=False))
        elif args.command == 'finish':
            print(json.dumps(finish(source,store,args.operation_id,args.outcome,load_json(args.live) if args.live else None),ensure_ascii=False))
        elif args.command == 'protect':
            key = 'codex:local:'+args.id
            with store.transaction():
                old = store.get(key)
                old.update(protected=True,protection_reason='manual_confirmation')
                store.save(key,old)
            print(json.dumps({'id':args.id,'protected':True}))
        elif args.command == 'approve':
            write_json(args.output,approve_title(store,args.id,args.title))
            print(json.dumps({'output':args.output,'protect_after':True}))
        elif args.command == 'restore-plan':
            write_json(args.output,restore_plan(source,store,args.operation_id))
            print(json.dumps({'output':args.output}))
        elif args.command == 'history':
            print(json.dumps([json.loads(r['value']) for r in store.db.execute('SELECT value FROM journal ORDER BY rowid')],ensure_ascii=False,indent=2))
        elif args.command == 'notify':
            print(json.dumps(notifications(store,load_json(args.report)),ensure_ascii=False))
        return 0
    except (ValueError,sqlite3.Error,OSError,KeyError,TypeError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1
    finally:
        if source:
            source.close()
        store.close()


if __name__ == '__main__':
    sys.exit(main())
