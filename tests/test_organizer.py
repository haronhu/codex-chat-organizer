import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'skills/codex-chat-organizer/scripts/organizer.py'
spec = importlib.util.spec_from_file_location('organizer', SCRIPT)
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)


class FakeSource:
    def __init__(self):
        self.threads = [{'id':'test-id','title':'帮我设置定时会话整理','first_user_message':'帮我设置定时会话整理','status':'completed','watermark':1,'archived':0,'kind':'codex','hostId':'local'}]
        self.content = {'user_intents':['帮我设置定时会话整理'],'recent_outcomes':['已检查工具能力']}
        self.reads = 0
    def inventory(self, include_archived=False):
        return copy.deepcopy([x for x in self.threads if include_archived or not x['archived']])
    def get(self, thread_id):
        return next((x.copy() for x in self.threads if x['id']==thread_id),None)
    def evidence(self, thread):
        self.reads += 1
        return copy.deepcopy(self.content)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = o.Store(Path(self.temp.name)/'state.sqlite')
        self.source = FakeSource()
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def plan(self, **kwargs):
        candidate = o.scan(self.source,self.store,full=True)['candidates'][0]
        decision = {'id':candidate['id'],'kind':'codex','hostId':'local','evidence_hash':candidate['evidence_hash'],'category':'工具自动化','title':'[工具自动化] 配置会话整理','confidence':0.95,'multi_topic':False,'reason':'同一工具配置目标'}
        decision.update(kwargs)
        return o.make_plan(self.store,[decision])
    def live(self):
        t = self.source.threads[0]
        return {'id':t['id'],'title':t['title'],'status':'idle','kind':'codex','hostId':'local','checked_at':time.time()}
    def success(self):
        action = self.plan()['actions'][0]
        op = o.begin(self.source,self.store,action,self.live())
        self.source.threads[0]['title'] = action['new_title']
        return o.finish(self.source,self.store,op['operation_id'],'success',self.live())
    def test_scan_never_changes_live_titles(self):
        before = copy.deepcopy(self.source.threads)
        self.plan()
        self.assertEqual(self.source.threads,before)
        self.assertEqual(self.store.pending(),[])
    def test_initial_scan_contains_all_candidates(self):
        self.source.threads += [{**self.source.threads[0],'id':str(i)} for i in range(120)]
        self.assertEqual(len(o.scan(self.source,self.store,full=True)['candidates']),121)
    def test_processed_metadata_noop_does_not_read_content(self):
        self.plan()
        before = self.source.reads
        report = o.scan(self.source,self.store)
        self.assertEqual(report['candidates'],[])
        self.assertEqual(self.source.reads,before)
    def test_new_candidate_retry_survives_preview_only(self):
        self.assertEqual(len(o.scan(self.source,self.store)['candidates']),1)
        self.assertEqual(len(o.scan(self.source,self.store)['candidates']),1)
    def test_planned_action_survives_next_incremental_scan(self):
        plan = self.plan()
        report = o.scan(self.source,self.store)
        self.assertEqual(report['ready_actions'],plan['actions'])
        self.assertEqual(report['candidates'],[])
    def test_live_inventory_active_overrides_completed_history(self):
        live = {'checked_at':time.time(),'threads':[{'id':'test-id','kind':'codex','hostId':'local','status':'active'}]}
        report = o.scan(self.source,self.store,live_inventory=live)
        self.assertEqual(report['candidates'],[])
        self.assertEqual(self.source.reads,0)
    def test_stale_live_inventory_rejected(self):
        with self.assertRaises(ValueError):
            o.scan(self.source,self.store,live_inventory={'checked_at':time.time()-90,'threads':[]})
    def test_not_loaded_does_not_authorize_write(self):
        action = self.plan()['actions'][0]
        with self.assertRaises(ValueError):
            o.begin(self.source,self.store,action,{**self.live(),'status':'notLoaded'})
    def test_trivial_new_user_turn_does_not_reclassify(self):
        self.plan()
        self.source.threads[0]['watermark'] += 1
        self.source.content['user_intents'].append('好的')
        self.assertEqual(o.scan(self.source,self.store)['candidates'],[])
    def test_trivial_change_refreshes_ready_action_watermark(self):
        self.plan()
        self.source.threads[0]['watermark']+=1
        self.source.content['user_intents'].append('好的')
        report=o.scan(self.source,self.store)
        action=report['ready_actions'][0]
        self.assertEqual(action['watermark'],2)
        self.assertEqual(o.begin(self.source,self.store,action,self.live())['state'],'pending')
    def test_meaningful_change_reclassifies(self):
        self.plan()
        self.source.threads[0]['watermark'] += 1
        self.source.content['user_intents'].append('接下来改为实现一个不同目标的可视化原型，首页要展示血压和血糖趋势以及家庭成员信息')
        self.assertEqual(len(o.scan(self.source,self.store)['candidates']),1)
    def test_active_and_unknown_skipped(self):
        for status in ('active','inProgress','waitingForApproval','unknown'):
            self.source.threads[0]['status'] = status
            self.assertEqual(o.scan(self.source,self.store,full=True)['candidates'],[])
        self.assertEqual(self.source.reads,0)
    def test_current_thread_excluded(self):
        self.assertEqual(o.scan(self.source,self.store,exclude=['test-id'])['candidates'],[])
    def test_archived_opt_in(self):
        self.source.threads[0]['archived']=1
        self.assertEqual(o.scan(self.source,self.store)['inventory_count'],0)
        self.assertEqual(o.scan(self.source,self.store,include_archived=True)['inventory_count'],1)
    def test_custom_title_suggestion_not_action(self):
        self.source.threads[0]['title']='我的固定标题'
        plan = self.plan()
        self.assertEqual(plan['actions'],[])
        self.assertEqual(plan['suggestions'][0]['blocked_reason'],'existing_custom_title_needs_confirmation')
    def test_external_change_locks_after_preview(self):
        self.plan()
        self.source.threads[0]['title']='手动改过的标题'
        report = o.scan(self.source,self.store,full=True)
        self.assertEqual(report['candidates'],[])
        self.assertTrue(self.store.get('codex:local:test-id')['protected'])
    def test_external_change_locks_after_success(self):
        self.success()
        self.source.threads[0]['title']='人工确认标题'
        self.assertEqual(o.scan(self.source,self.store,full=True)['candidates'],[])
    def test_explicit_protection_cannot_be_bypassed_by_full(self):
        self.store.save('codex:local:test-id',{'protected':True})
        self.assertEqual(o.scan(self.source,self.store,full=True)['candidates'],[])
    def test_low_confidence_and_multi_topic_blocked(self):
        for decision in ({'confidence':0.89},{'multi_topic':True},{'multi_topic':None},{'category':'不存在'}):
            self.assertFalse(self.plan(**decision)['actions'])
    def test_bad_confidence_rejected(self):
        for confidence in (True,-1,1.1,float('nan'),float('inf'),'1'):
            with self.assertRaises(ValueError):
                self.plan(confidence=confidence)
    def test_unsafe_and_overlong_title_blocked(self):
        for title in ('不带前缀','[工具自动化] '+'很长'*30,'[工具自动化] token=secret','[工具自动化] https://example.com'):
            self.assertFalse(self.plan(title=title)['actions'])
    def test_stale_hash_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(evidence_hash='stale')
    def test_batch_failure_rolls_back_approved_actions(self):
        candidate = o.scan(self.source,self.store,full=True)['candidates'][0]
        d={'id':'test-id','evidence_hash':candidate['evidence_hash'],'category':'工具自动化','title':'[工具自动化] 整理','confidence':0.99,'multi_topic':False}
        with self.assertRaises(ValueError):
            o.make_plan(self.store,[d,d])
        self.assertNotIn('approved_action',self.store.get('codex:local:test-id'))
    def test_pending_written_before_external_mutation(self):
        action=self.plan()['actions'][0]
        op=o.begin(self.source,self.store,action,self.live())
        self.assertEqual(self.source.threads[0]['title'],action['old_title'])
        self.assertEqual(self.store.pending()[0]['old_title'],action['old_title'])
        self.assertEqual(op['state'],'pending')
        with self.assertRaises(ValueError):
            o.begin(self.source,self.store,action,self.live())
    def test_active_live_check_blocks_even_with_stale_local_idle(self):
        action=self.plan()['actions'][0]
        live=self.live();live['status']='active'
        with self.assertRaises(ValueError):
            o.begin(self.source,self.store,action,live)
    def test_live_check_expiry_and_title_conflict(self):
        action=self.plan()['actions'][0]
        for update in ({'checked_at':time.time()-60},{'checked_at':float('nan')},{'title':'不同标题'}):
            with self.assertRaises(ValueError):
                o.begin(self.source,self.store,action,{**self.live(),**update})
    def test_content_race_blocks(self):
        action=self.plan()['actions'][0]
        self.source.threads[0]['watermark'] += 1
        with self.assertRaises(ValueError):
            o.begin(self.source,self.store,action,self.live())
    def test_tampered_action_blocks(self):
        action=self.plan()['actions'][0];action['new_title']='篡改标题'
        with self.assertRaises(ValueError):
            o.begin(self.source,self.store,action,self.live())
    def test_success_must_be_verified(self):
        action=self.plan()['actions'][0]
        op=o.begin(self.source,self.store,action,self.live())
        with self.assertRaises(ValueError):
            o.finish(self.source,self.store,op['operation_id'],'success',self.live())
        self.assertEqual(len(self.store.pending()),1)
    def test_uncertain_failure_preserves_pending(self):
        action=self.plan()['actions'][0];op=o.begin(self.source,self.store,action,self.live())
        self.source.threads[0]['title']='第三种标题'
        with self.assertRaises(ValueError):
            o.finish(self.source,self.store,op['operation_id'],'failed')
        self.assertEqual(len(self.store.pending()),1)
    def test_failed_operation_becomes_retryable(self):
        action=self.plan()['actions'][0];op=o.begin(self.source,self.store,action,self.live())
        o.finish(self.source,self.store,op['operation_id'],'failed')
        self.assertEqual(len(o.scan(self.source,self.store)['candidates']),1)
    def test_restore_then_lock_prevents_oscillation(self):
        record=self.success()
        action=o.restore_plan(self.source,self.store,record['operation_id'])
        op=o.begin(self.source,self.store,action,self.live())
        self.source.threads[0]['title']=record['old_title']
        o.finish(self.source,self.store,op['operation_id'],'success',self.live())
        self.assertEqual(o.scan(self.source,self.store,full=True)['candidates'],[])
    def test_restore_refuses_external_title_change(self):
        record=self.success();self.source.threads[0]['title']='人工标题'
        with self.assertRaises(ValueError):
            o.restore_plan(self.source,self.store,record['operation_id'])
    def test_manual_approval_journal_then_protect(self):
        self.source.threads[0]['title']='旧手动标题'
        self.plan()
        action=o.approve_title(self.store,'test-id','用户确认的标题')
        op=o.begin(self.source,self.store,action,self.live())
        self.source.threads[0]['title']=action['new_title']
        o.finish(self.source,self.store,op['operation_id'],'success',self.live())
        self.assertTrue(self.store.get('codex:local:test-id')['protected'])
    def test_approval_cannot_unlock_protected_title(self):
        self.plan()
        old=self.store.get('codex:local:test-id');old['protected']=True;self.store.save('codex:local:test-id',old)
        with self.assertRaises(ValueError):o.approve_title(self.store,'test-id','其他标题')
    def test_new_scan_invalidates_stale_approved_action(self):
        old_action=self.plan()['actions'][0]
        self.source.threads[0]['watermark']+=1
        self.source.content['user_intents'].append('切换到一个全新的目标：调研数据库故障恢复策略并实现服务端接口测试')
        o.scan(self.source,self.store)
        with self.assertRaises(ValueError):o.begin(self.source,self.store,old_action,self.live())
    def test_notice_noop_and_dedup(self):
        self.assertFalse(o.notifications(self.store,{})['notify'])
        a={'suggestions':[{'id':'test-id','new_title':'建议','watermark':1,'evidence_hash':'a'}]}
        self.assertTrue(o.notifications(self.store,a)['notify'])
        a['suggestions'][0].update(watermark=2,evidence_hash='b')
        self.assertFalse(o.notifications(self.store,a)['notify'])
        a['suggestions'][0]['new_title']='新建议'
        self.assertTrue(o.notifications(self.store,a)['notify'])
    def test_safe_text_removes_ambient_and_credentials(self):
        text='<rules>password=bad</rules> ## My request: 帮我设置定时任务 token=abcdef https://private.example/path'
        cleaned=o.safe_text(text)
        self.assertNotIn('password',cleaned);self.assertNotIn('abcdef',cleaned);self.assertNotIn('private.example',cleaned)
        self.assertTrue(cleaned.startswith('帮我设置定时任务'))
    def test_generated_title_recognizes_truncated_request(self):
        self.assertTrue(o.generated_title('在这台机器上安装 GitHub CLI (gh)…','在这台机器上安装 GitHub CLI (`gh`)，验证其可用性'))
        self.assertFalse(o.generated_title('固定标题','在这台机器上安装 GitHub CLI'))


class CurrentRenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = o.Store(Path(self.temp.name)/'state.sqlite')
        self.source = FakeSource()
        self.caller = patch.dict(o.os.environ,{'CODEX_THREAD_ID':'test-id'})
        self.caller.start()
        self.context = {'surface':'main','binding_source':'caller','caller_thread_id':'test-id','main_thread_id':'test-id','kind':'codex','hostId':'local'}
    def tearDown(self):
        self.caller.stop();self.store.close();self.temp.cleanup()
    def live(self, status='idle'):
        t = self.source.threads[0]
        return {'id':t['id'],'title':t['title'],'status':status,'kind':'codex','hostId':'local','checked_at':time.time()}
    def plan(self):
        return o.current_plan(self.source,self.store,self.context,'[工具自动化] 当前主会话')
    def test_exact_shortcut_with_optional_title(self):
        self.assertEqual(o.shortcut_request('改一个会话名'),{'mode':'current','title':None})
        self.assertEqual(o.shortcut_request('改一个会话名：我的新标题')['title'],'我的新标题')
        self.assertEqual(o.shortcut_request('给当前会话改名')['mode'],'current')
    def test_feature_requests_and_quotes_not_execution_consent(self):
        for request in ('加一个功能：发送“改一个会话名”的时候修改主会话','他说“改一个会话名”','不要改一个会话名','改一个会话名并改所有会话'):
            self.assertIsNone(o.shortcut_request(request))
    def test_main_target_uses_actual_caller(self):
        self.assertEqual(o.resolve_current_target(self.context),'test-id')
        with self.assertRaises(ValueError):
            o.resolve_current_target({**self.context,'main_thread_id':'wrong'})
    def test_sidebar_requires_distinct_explicit_main_binding(self):
        with patch.dict(o.os.environ,{'CODEX_THREAD_ID':'side-id'}):
            context={**self.context,'surface':'sidebar','caller_thread_id':'side-id','binding_source':'host_context'}
            self.assertEqual(o.resolve_current_target(context),'test-id')
            self.assertEqual(o.resolve_current_target({**context,'binding_source':'user_reference'}),'test-id')
            for update in ({'main_thread_id':'side-id'},{'binding_source':'recency'},{'main_thread_id':None}):
                with self.assertRaises(ValueError):o.resolve_current_target({**context,**update})
    def test_missing_caller_or_unknown_surface_fails_closed(self):
        for update in ({'caller_thread_id':'other'},{'surface':'unknown'},{'hostId':'remote'},{'kind':'chatgpt'}):
            with self.assertRaises(ValueError):o.resolve_current_target({**self.context,**update})
        with patch.dict(o.os.environ,{'CODEX_THREAD_ID':''}):
            with self.assertRaises(ValueError):o.resolve_current_target(self.context)
    def test_single_plan_does_not_scan_other_chats(self):
        with patch.object(self.source,'inventory',side_effect=AssertionError('Must not scan')):
            action=self.plan()
        self.assertEqual(action['id'],'test-id')
        self.assertEqual(self.source.reads,0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM observed').fetchone()[0],1)
    def test_active_current_rename_records_and_verifies(self):
        self.source.threads[0]['status']='inProgress'
        action=self.plan()
        op=o.begin(self.source,self.store,action,self.live('active'))
        self.assertEqual(self.source.threads[0]['title'],action['old_title'])
        self.assertEqual(self.store.pending()[0]['old_title'],action['old_title'])
        self.source.threads[0]['title']=action['new_title']
        record=o.finish(self.source,self.store,op['operation_id'],'success',self.live('active'))
        self.assertEqual(record['state'],'success')
        self.assertTrue(self.store.get('codex:local:test-id')['protected'])
    def test_sidebar_renames_bound_main_not_itself(self):
        self.source.threads.append({**self.source.threads[0],'id':'side-id','title':'侧边栏聊天'})
        with patch.dict(o.os.environ,{'CODEX_THREAD_ID':'side-id'}):
            context={**self.context,'surface':'sidebar','caller_thread_id':'side-id','binding_source':'host_context'}
            action=o.current_plan(self.source,self.store,context,'主会话新标题')
            op=o.begin(self.source,self.store,action,self.live('active'))
            self.source.threads[0]['title']=action['new_title']
            o.finish(self.source,self.store,op['operation_id'],'success',self.live('active'))
        self.assertEqual(self.source.threads[0]['title'],'主会话新标题')
        self.assertEqual(self.source.threads[1]['title'],'侧边栏聊天')
    def test_explicit_current_rename_preserves_batch_protection(self):
        self.store.save('codex:local:test-id',{'protected':True,'protection_reason':'manual_confirmation'})
        action=self.plan();op=o.begin(self.source,self.store,action,self.live())
        self.source.threads[0]['title']=action['new_title']
        o.finish(self.source,self.store,op['operation_id'],'success',self.live())
        self.assertEqual(o.scan(self.source,self.store,full=True)['candidates'],[])
    def test_caller_change_between_plan_and_begin_blocks(self):
        action=self.plan()
        with patch.dict(o.os.environ,{'CODEX_THREAD_ID':'other'}):
            with self.assertRaises(ValueError):o.begin(self.source,self.store,action,self.live())
        self.assertFalse(self.store.pending())
    def test_unknown_or_unloaded_still_blocks(self):
        action=self.plan()
        for status in ('notLoaded','unknown','waitingForApproval'):
            with self.assertRaises(ValueError):o.begin(self.source,self.store,action,self.live(status))
    def test_protected_batch_rename_remains_blocked(self):
        action=self.plan();action['operation']='rename'
        old=self.store.get('codex:local:test-id');old.update(protected=True,approved_action=action);self.store.save('codex:local:test-id',old)
        with self.assertRaises(ValueError):o.begin(self.source,self.store,action,self.live())
    def test_unchanged_title_no_write(self):
        result=o.current_plan(self.source,self.store,self.context,self.source.threads[0]['title'])
        self.assertEqual(result['status'],'unchanged');self.assertFalse(self.store.pending())
        self.assertFalse(self.store.get('codex:local:test-id'))
    def test_pending_or_archived_main_not_overwritten(self):
        action=self.plan();o.begin(self.source,self.store,action,self.live())
        with self.assertRaises(ValueError):self.plan()
        self.source.threads[0]['archived']=1
        with self.assertRaises(ValueError):self.plan()
    def test_title_conflict_and_unverified_success_block(self):
        action=self.plan();self.source.threads[0]['title']='人工新标题'
        with self.assertRaises(ValueError):o.begin(self.source,self.store,action,self.live())
        action=self.plan();op=o.begin(self.source,self.store,action,self.live())
        with self.assertRaises(ValueError):o.finish(self.source,self.store,op['operation_id'],'success',self.live())
        self.assertEqual(len(self.store.pending()),1)
    def test_current_history_can_restore_after_completion(self):
        action=self.plan();op=o.begin(self.source,self.store,action,self.live())
        self.source.threads[0]['title']=action['new_title']
        o.finish(self.source,self.store,op['operation_id'],'success',self.live())
        restore=o.restore_plan(self.source,self.store,op['operation_id'])
        restore_op=o.begin(self.source,self.store,restore,self.live())
        self.source.threads[0]['title']=action['old_title']
        o.finish(self.source,self.store,restore_op['operation_id'],'success',self.live())
        self.assertEqual(self.source.threads[0]['title'],action['old_title'])
        self.assertTrue(self.store.get('codex:local:test-id')['protected'])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        db=sqlite3.connect(self.home/'state_5.sqlite')
        db.execute('CREATE TABLE threads (id TEXT,title TEXT,name TEXT,rollout_path TEXT,first_user_message TEXT,archived INTEGER,source TEXT,thread_source TEXT,updated_at INTEGER)')
        for id,source,origin,archived in [('one','vscode','user',0),('two','cli','user',1),('sub','vscode','subagent',0),('auto','vscode','automation',0)]:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)',(id,'旧默认标题','显示名称','none','首个问题',archived,source,origin,1))
        db.commit();db.close()
        db=sqlite3.connect(self.home/'thread_history_1.sqlite')
        db.executescript('CREATE TABLE thread_turns(thread_id TEXT,rollout_ordinal INTEGER,status TEXT); CREATE TABLE thread_items(thread_id TEXT,rollout_ordinal INTEGER,item_type TEXT,item_json TEXT);')
        db.execute("INSERT INTO thread_turns VALUES('one',1,'completed')")
        for i in range(6):
            value={'type':'userMessage','content':[{'type':'text','text':f'问题{i}'}]}
            db.execute('INSERT INTO thread_items VALUES(?,?,?,?)',('one',i*100,'userMessage',json.dumps(value)))
            for j in range(40):
                db.execute('INSERT INTO thread_items VALUES(?,?,?,?)',('one',i*100+j+1,'agentMessage',json.dumps({'text':'中间输出','phase':'commentary'})))
        db.commit();db.close();self.source=o.LocalSource(self.home)
    def tearDown(self):
        self.source.close();self.temp.cleanup()
    def test_display_name_and_scope(self):
        self.assertEqual([x['id'] for x in self.source.inventory()],['one'])
        self.assertEqual(self.source.inventory()[0]['title'],'显示名称')
        self.assertEqual(len(self.source.inventory(True)),2)
    def test_read_only_enforced(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.source.db.execute('UPDATE threads SET title=?',('broken',))
    def test_single_get_does_not_enumerate_all_threads(self):
        with patch.object(self.source,'inventory',side_effect=AssertionError('Must not scan')):
            self.assertEqual(self.source.get('one')['title'],'显示名称')
            self.assertIsNone(self.source.get('sub'))
    def test_current_cli_plan_and_same_title_noop(self):
        context={'surface':'main','binding_source':'caller','caller_thread_id':'one','main_thread_id':'one','kind':'codex','hostId':'local'}
        context_path=self.home/'context.json';context_path.write_text(json.dumps(context))
        output=self.home/'current-plan.json';state=self.home/'organizer.sqlite'
        cmd=[sys.executable,str(SCRIPT),'--codex-home',str(self.home),'--state',str(state),'current-plan','--context',str(context_path),'--request','改一个会话名','--title','主会话新标题','--output',str(output)]
        with patch.dict(o.os.environ,{'CODEX_THREAD_ID':'one'}):
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            action=json.loads(output.read_text());self.assertEqual(action['id'],'one')
            self.assertEqual(action['operation'],'current_rename')
            self.assertEqual(self.source.get('one')['title'],'显示名称')
            self.assertEqual(self.source.get('two')['title'],'显示名称')
            cmd[cmd.index('--title')+1]='显示名称'
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(output.read_text())['status'],'unchanged')
            cmd[cmd.index('--request')+1]='请新增一个发送改一个会话名的功能'
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            cmd[cmd.index('--request')+1]='改一个会话名：用户指定标题'
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
        store=o.Store(state)
        self.assertFalse(store.pending());store.close()
    def test_user_messages_not_drowned_out_by_assistant_chatter(self):
        t=self.source.get('one');e=self.source.evidence(t)
        self.assertEqual(e['user_intents'],['首个问题','问题2','问题3','问题4','问题5'])
    def test_missing_history_fails_closed(self):
        with self.assertRaises(ValueError):
            self.source.evidence(self.source.get('two'))
    def test_unsupported_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(Path(tmp)/'state_99.sqlite');db.execute('CREATE TABLE threads(id TEXT)');db.close()
            with self.assertRaises(ValueError):o.LocalSource(tmp)


class CLITests(unittest.TestCase):
    def test_install_is_idempotent_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd=[sys.executable,str(ROOT/'install.py'),'--destination',tmp]
            self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,0)
            self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,0)
            target=Path(tmp)/'codex-chat-organizer';target.unlink();target.mkdir();(target/'sentinel').write_text('keep')
            self.assertNotEqual(subprocess.run(cmd,capture_output=True).returncode,0)
            self.assertEqual((target/'sentinel').read_text(),'keep')
    def test_schedule_prompt_has_no_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.sqlite'
            result=subprocess.run([sys.executable,str(SCRIPT),'--state',str(state),'schedule-prompt'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0);self.assertIn('只做预览',result.stdout);self.assertFalse(state.exists())
    def test_atomic_json_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'preview.json';o.write_json(p,{'test':'中文'})
            self.assertEqual(o.load_json(p),{'test':'中文'})
            self.assertEqual(p.stat().st_mode & 0o777,0o600)
            self.assertEqual(list(Path(tmp).glob('*.tmp')),[])


if __name__=='__main__':
    unittest.main()
