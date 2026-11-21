import json,copy
from app.database import Session
from app.models import Ticket,Event,Delivery,Rule,Audit,ChannelState,User
from app.seed import WORKFLOW,PAYMENTS,TRADE
from app.services.ingestion import ingest
from app.services.routing import pump_events
from app.services.delivery import process_deliveries,RETRY_SECONDS
from app.services.conditions import matches,canonical,validate


def test_additional_channels_are_optional_and_deliver_once(login):
    client = login(2)
    assert client.get('/api/me').json()['preferred_channels'] == []
    settings = {'preferred_channels': ['sms', 'phone', 'sms']}
    assert client.put('/api/preferences', json=settings).status_code == 200
    assert client.put('/api/preferences', json={'timezone': 'UTC'}).status_code == 200
    assert client.get('/api/me').json()['preferred_channels'] == ['phone', 'sms']
    for invalid in (['email'], ['ui'], ['unknown'], 'sms'):
        assert client.put('/api/preferences', json={'preferred_channels': invalid}).status_code == 422
    task = client.post('/api/tickets', json={'team_id': 1, 'title': 'Delivery check'}).json()
    with Session() as db:
        jobs = db.query(Delivery).filter_by(user_id=2, ticket_id=task['id'], kind='new_ticket').all()
        assert sorted((job.channel, job.is_primary) for job in jobs) == [
            ('phone', False), ('sms', False), ('ui', True)]
    assert client.put('/api/preferences', json={'preferred_channels': []}).status_code == 200
    profile = client.get('/api/me').json()
    assert (profile['primary_channel'], profile['backup_channel'], profile['preferred_channels']) == ('ui', 'email', [])


def test_seed_matches_all_appendix_schemas_and_falls_through_teams():
    with Session() as db:
        events=db.query(Event).order_by(Event.id).all()
        assert len(events)==3 and all(x.state=='matched' for x in events)
        assert [x.ticket_id for x in events]==[1,2,3]
        assert [len(x.route_trace) for x in events]==[2,1,3]
        assert events[0].route_trace[0]['team_id']==1
        assert events[0].route_trace[-1]['team_id']==2
        assert db.get(Ticket,1).schema=='WORKFLOW_FAILED'
        assert db.get(Ticket,2).severity=='critical'
        assert db.get(Ticket,3).first_payload['payload']['notional']==25000000


def test_flood_500_events_is_one_ticket_and_one_initial_notification():
    with Session() as db:
        s=db.query(__import__('app.models',fromlist=['Source']).Source).filter_by(slug='demo').one()
        old=db.get(Ticket,2).occurrences
        data=json.dumps(PAYMENTS)
        for _ in range(500):ingest(db,s,data)
        db.commit()
        while pump_events(db,limit=500):db.commit()
        matching=db.query(Ticket).filter_by(title='High error rate · payments-api').all()
        assert len(matching)==1
        assert matching[0].occurrences==old+500
        assert len(matching[0].samples)<=20
        assert db.query(Event).filter_by(state='matched',ticket_id=2).count()==501
        # Delivery jobs are not created 500 times for this incident.
        assert db.query(Delivery).filter_by(ticket_id=2,kind='new_ticket').count()<=4


def test_unmatched_event_survives_and_invalid_payload_is_rejected(login):
    client=login(6)
    r=client.post('/api/debug/generate',json={'count':1,'event':{'brandNew':{'kind':'NO_RULE','nested':[1,{'x':2}]}}})
    assert r.status_code==200
    with Session() as db:
        unmatched=db.query(Event).filter_by(state='unmatched').one()
        assert len(unmatched.route_trace)==4
        assert unmatched.raw=='{"brandNew": {"kind": "NO_RULE", "nested": [1, {"x": 2}]}}'
        assert unmatched.ticket_id is None
    r=client.post('/api/ingest/webhook',content=b'{ invalid json')
    assert r.status_code==200
    with Session() as db:assert db.query(Event).filter_by(state='rejected').count()==1


def test_log_line_source_parser_and_pricing_grouping(login):
    client=login(1)
    a='WARN pricing-engine-v2 instrument=EURUSD High cache latency'
    b='WARN pricing-engine-v2 instrument=EURUSD Cache eviction retries'
    r1=client.post('/api/ingest/logs',content=a.encode())
    r2=client.post('/api/ingest/logs',content=b.encode())
    assert r1.status_code==200 and r2.status_code==200
    with Session() as db:
        x=db.get(Event,r1.json()['event_id']);y=db.get(Event,r2.json()['event_id'])
        assert x.state=='matched' and y.state=='matched'
        assert x.ticket_id==y.ticket_id
        t=db.get(Ticket,x.ticket_id)
        assert t.occurrences==2 and t.team_id==4


def test_recovery_to_done_then_relapse_before_approval(login):
    client=login(6)
    recovery=copy.deepcopy(PAYMENTS);recovery['status']='resolved'
    client.post('/api/debug/generate',json={'event':recovery})
    with Session() as db:
        ticket=db.get(Ticket,2)
        assert ticket.state=='done' and ticket.incident_status=='resolved'
        assert 'attention' in ticket.flags and ticket.done_by is None
    client.post('/api/debug/generate',json={'event':PAYMENTS})
    with Session() as db:
        t=db.get(Ticket,2)
        assert t.state=='in_progress' and t.incident_status=='firing'
        assert 'relapse' in t.flags and t.occurrences==2


def test_manager_can_approve_own_incident(login):
    client = login(1)
    path = '/api/tickets/2'
    assert client.post(path + '/move', json={'state':'done'}).status_code == 409
    complete_runbook(login, 2, 3)
    client = login(1)
    assert client.post(path + '/move', json={'state': 'done'}).status_code == 200
    ticket = client.get(path).json()
    assert ticket['type'] == 'incident' and ticket['permissions']['can_approve']
    assert client.post(path + '/approve').status_code == 200
    with Session() as db:
        incident = db.get(Ticket, 2)
        assert (incident.state, incident.done_by, incident.approved_by) == ('archived', 1, 1)
        assert incident.active_key is None


def complete_runbook(login, ticket_id, worker):
    client = login(worker)
    path = f'/api/tickets/{ticket_id}'
    ticket = client.get(path).json()
    if ticket['state'] == 'acknowledged':
        assert client.post(path + '/move', json={'state': 'in_progress'}).status_code == 200
    assert client.post(path + '/move', json={'state': 'done'}).status_code == 409
    for index, step in enumerate(ticket['runbook_steps']):
        url = f'{path}/steps/{index}'
        assert client.post(url + '/run').status_code == 200
        if step.get('approval') == 'required':
            assert login(1).post(url + '/approve').status_code == 200
            client = login(worker)
            assert client.get(path).json()['runbook_steps'][index]['status'] == 'done'


def test_maintainer_needs_another_approver(login):
    complete_runbook(login, 2, 3)
    client=login(3)
    assert client.post('/api/tickets/2/move',json={'state':'done'}).status_code==200
    response=client.post('/api/tickets/2/approve')
    assert response.status_code==403 and 'Separation of duties' in response.text
    client=login(1)  # Team manager approves the maintainer's resolution
    assert client.post('/api/tickets/2/approve').status_code==200
    with Session() as db:
        t=db.get(Ticket,2)
        assert t.state=='archived' and t.active_key is None and t.approved_by==1
    client=login(2)
    assert client.get('/api/archive').status_code==403
    assert client.get('/api/tickets/2').status_code==403 # was not personally handling


def test_team_manager_can_complete_and_approve_team_task(login):
    client = login(2)
    task = client.post('/api/tickets', json={'team_id': 1, 'title': 'Review thresholds'})
    task.raise_for_status()
    path = '/api/tickets/' + str(task.json()['id'])
    client = login(1)
    assert client.post(path + '/move', json={'state': 'done'}).status_code == 200
    assert client.get(path).json()['permissions']['can_approve']
    assert client.post(path + '/approve').status_code == 200
    with Session() as db:
        saved = db.get(Ticket, task.json()['id'])
        assert (saved.state, saved.done_by, saved.approved_by) == ('archived', 1, 1)


def test_risky_runbook_action_is_manager_only(login):
    manager = login(1)
    assert manager.post('/api/tickets/1/steps/0/run').status_code == 403
    client=login(2)
    assert client.post('/api/tickets/1/steps/2/run').status_code == 409
    assert client.post('/api/tickets/1/move', json={'state':'in_progress'}).status_code == 200
    assert client.post('/api/tickets/1/steps/2/run').status_code==200
    with Session() as db:
        t=db.get(Ticket,1)
        assert t.runbook_steps[2]['status']=='waiting_approval'
    client=login(3)
    assert client.post('/api/tickets/1/steps/2/approve').status_code==403
    assert client.post('/api/tickets/1/steps/2/reject').status_code==403
    client=login(1)
    assert client.post('/api/tickets/1/steps/2/reject').status_code==200
    assert client.post('/api/tickets/1/steps/2/approve').status_code==409
    assert login(2).post('/api/tickets/1/steps/2/run').status_code==200
    client=login(1)
    assert client.post('/api/tickets/1/steps/2/approve').status_code==200
    assert client.post('/api/tickets/1/steps/2/approve').status_code==409
    with Session() as db:
        assert db.get(Ticket,1).runbook_steps[2]['status']=='done'


def test_later_steps_wait_for_approved_completion(login):
    with Session() as db:
        ticket = db.get(Ticket, 1)
        ticket.runbook_steps = [dict(ticket.runbook_steps[0], approval='required'), *ticket.runbook_steps[1:]]
        db.commit()
    path = '/api/tickets/1'
    first = path + '/steps/0'
    later = path + '/steps/1/run'
    worker = login(2)
    assert worker.post(path + '/move', json={'state': 'in_progress'}).status_code == 200
    assert worker.get(path).json()['approval_gate_index'] == 0
    assert worker.post(later).status_code == 409
    assert worker.post(first + '/run').status_code == 200
    assert worker.post(path + '/steps/2/run').status_code == 409
    assert login(1).post(first + '/reject').status_code == 200
    worker = login(2)
    assert worker.post(later).status_code == 409
    assert worker.post(first + '/run').status_code == 200
    assert login(1).post(first + '/approve').status_code == 200
    worker = login(2)
    assert worker.get(path).json()['approval_gate_index'] == 2
    assert worker.post(later).status_code == 200


def test_manager_only_rule_runbook_and_proposals(login):
    client=login(3) # BM
    assert client.post('/api/rules',json={'team_id':1,'name':'No access','condition':{}}).status_code==403
    assert client.post('/api/runbooks',json={'team_id':1,'name':'No access','source_text':'Text'}).status_code==403
    client=login(2)
    body={'team_id':1,'target_type':'rule','target_id':None,'description':'Please route risk failures'}
    one=client.post('/api/proposals',json=body)
    assert one.status_code==200
    assert client.post('/api/proposals',json=body).status_code==409
    client=login(3)
    assert client.post(f"/api/proposals/{one.json()['id']}/decision",json={'decision':'accepted'}).status_code==403
    client=login(1)
    assert client.post(f"/api/proposals/{one.json()['id']}/decision",json={'decision':'rejected','comment':'Revise condition'}).status_code==200
    client=login(2)
    two=client.post('/api/proposals',json={**body,'description':'Updated rule condition'})
    assert two.status_code==200 and two.json()['id']!=one.json()['id']
    with Session() as db:
        from app.models import Proposal
        assert db.get(Proposal,two.json()['id']).revised_from==one.json()['id']


def test_archive_critical_only_default_toggle_errors_and_handled_history(login):
    # This archive fixture has no runbook action that changes its severity.
    with Session() as db:
        db.get(Ticket,1).runbook_steps=[]
        db.commit()
    client=login(1)
    assert client.post('/api/tickets/1/move',json={'state':'done'}).status_code==200
    client=login(1)
    assert client.post('/api/tickets/1/approve').status_code==200
    default=client.get('/api/archive?team=2').json()
    expanded=client.get('/api/archive?team=2&include_errors=true').json()
    assert default==[]
    assert len(expanded)==1 and expanded[0]['id']==1
    client=login(2)
    assert client.get('/api/tickets/1').status_code==200
    assert any(t['id']==1 for t in client.get('/api/tickets?handled=true').json())
    assert client.get('/api/archive?include_errors=true').status_code==403


def test_admin_is_not_debug_and_debug_is_not_admin(login):
    client=login(5)
    assert client.get('/api/admin').status_code==200
    assert client.get('/api/debug').status_code==403
    assert client.post('/api/debug/generate',json={'count':1}).status_code==403
    client=login(6)
    assert client.get('/api/debug').status_code==200
    assert client.get('/api/admin').status_code==403
    assert client.post('/api/admin/sources',json={'name':'Injected','slug':'bad','type':'webhook'}).status_code==403
    assert client.post('/api/rules',json={'team_id':1,'name':'Not allowed'}).status_code==403


def test_personal_channel_must_have_distinct_backup(login):
    client=login(2)
    assert client.put('/api/preferences',json={'primary_channel':'email','backup_channel':'email'}).status_code==422
    assert client.put('/api/preferences',json={'primary_channel':'email','backup_channel':'ui'}).status_code==200
    assert client.get('/api/me').json()['backup_channel']=='ui'


def test_failed_primary_retries_then_distinct_backup(login):
    client=login(2)
    assert client.put('/api/preferences',json={'primary_channel':'email','backup_channel':'ui'}).status_code==200
    client=login(6)
    assert client.post('/api/debug/channel',json={'name':'email','failing':True}).status_code==200
    payload=copy.deepcopy(WORKFLOW);payload['workflowId']='WF-NOTIFY-UNIQUE'
    r=client.post('/api/debug/generate',json={'event':payload})
    assert r.status_code==200
    old=RETRY_SECONDS[:];RETRY_SECONDS[:]=[0,0,0,0,0]
    try:
        with Session() as db:
            t=db.query(Ticket).filter_by(title='Settlement failure · WF-NOTIFY-UNIQUE').one()
            primary=db.query(Delivery).filter_by(ticket_id=t.id,user_id=2,channel='email',is_primary=True).one()
            from app.models import now
            for _ in range(8):
                primary.available_at=now()
                process_deliveries(db);db.commit()
            assert primary.status=='failed' and primary.attempts==6
            fallback=db.query(Delivery).filter_by(fallback_for=primary.id).one()
            assert fallback.channel=='ui' and fallback.status=='sent'
    finally:RETRY_SECONDS[:]=old


def test_guest_board_access_does_not_grant_modification(login):
    client=login(7) # Rates only
    assert client.get('/api/board?team=1').status_code==403
    r=client.post('/api/access',json={'team_id':1,'reason':'Cross-team awareness'})
    assert r.status_code==200
    with Session() as db:
        from app.models import BoardAccess
        request_id=db.query(BoardAccess).filter_by(user_id=7,team_id=1).one().id
    client=login(1)
    assert client.post(f'/api/access/{request_id}/decision',json={'approved':True}).status_code==200
    client=login(7)
    assert client.get('/api/board?team=1').status_code==200
    assert client.post('/api/tickets/2/join').status_code==403
    assert client.post('/api/tickets/2/comment',json={'comment':'guest write'}).status_code==403


def test_rule_editor_unknown_nested_schema_at_runtime(login):
    client=login(1)
    cond={'all':[{'field':'envelope.signal.details.identifier','op':'gte','value':40},
                 {'any':[{'field':'envelope.facility.component','op':'eq','value':'risk-engine'},
                         {'field':'envelope.kind','op':'wildcard','value':'CUSTOM_*'}]}]}
    data={'team_id':1,'name':'New schema rule','priority':1,'condition':cond,
          'presentation':{'title':'New: {{envelope.facility.component}}','body':'{{envelope.signal.details.text}}'},
          'dedup_template':'{{envelope.facility.instance}}:{{envelope.kind}}'}
    assert client.post('/api/rules',json=data).status_code==200
    payload={'envelope':{'kind':'CUSTOM_UPSTREAM_FAILURE','facility':{'component':'risk-engine','instance':'risk-eu-4'},'signal':{'details':{'identifier':42,'text':'New schema matched'}}}}
    check=client.post('/api/rules/test',json={'team_id':1,'rule':data,'event':payload}).json()
    assert check['matched'] and check['title']=='New: risk-engine'
    client=login(6)
    assert client.post('/api/debug/generate',json={'event':payload}).status_code==200
    with Session() as db:
        ticket=db.query(Ticket).filter_by(title='New: risk-engine').one()
        assert ticket.team_id==1 and ticket.first_payload==payload


def test_semantic_markup_and_ui_assets(client):
    html=client.get('/').text
    js=client.get('/assets/app.js').text
    css=client.get('/assets/styles.css').text
    for id in ('app-header','app-nav','app-main','team-selector','kanban-board','counter-total','column-new','column-done',
               'recent-activity','rules-list','runbooks-list','debug-event-generator','delivery-log','ticket-detail','org-manager'):
        assert f'id="{id}"' in html
    assert 'EventSource(' in js and 'data-action' in html
    assert '--gold:' in css and '--navy:' in css and 'data-severity' in css


def test_delivery_digest_groups_multiple_tickets_in_one_window(login):
    client=login(2)
    assert client.put('/api/preferences',json={'primary_channel':'email','backup_channel':'ui','buffer_seconds':60}).status_code==200
    for i in range(3):
        assert client.post('/api/tickets',json={'team_id':2,'title':f'Independent task {i}'}).status_code==200
    with Session() as db:
        latest=[t.id for t in db.query(Ticket).filter(Ticket.title.like('Independent task %'))]
        jobs=db.query(Delivery).filter_by(user_id=2,channel='email',kind='new_ticket').all()
        new_jobs=[d for d in jobs if (d.content or {}).get('digest') and any(x['ticket_id'] in latest for x in d.content['entries'])]
        assert len(new_jobs)==1
        assert len(new_jobs[0].content['entries'])==3
        assert new_jobs[0].status=='held'


def test_manager_forced_delivery_respects_schedule_by_default(login):
    client=login(2)
    assert client.put('/api/preferences',json={'primary_channel':'email','backup_channel':'ui',
                          'schedule':{'enabled':True,'start':'23:50','end':'23:59','timezone':'Pacific/Honolulu'}}).status_code==200
    with Session() as db:
        from app.models import Policy
        db.add(Policy(team_id=2,severity='all',channel='email',kind='new_ticket',target='all',ignore_schedule=False));db.commit()
    assert client.post('/api/tickets',json={'team_id':2,'title':'Out of hours task'}).status_code==200
    with Session() as db:
        t=db.query(Ticket).filter_by(title='Out of hours task').one()
        job=db.query(Delivery).filter_by(user_id=2,ticket_id=t.id,channel='email',kind='new_ticket').one()
        assert job.status=='held'
        assert job.available_at.replace(tzinfo=__import__('datetime').timezone.utc)>__import__('datetime').datetime.now(__import__('datetime').timezone.utc)


def test_manager_signed_live_rule_config_is_atomic_and_auth_checked(login,tmp_path):
    client=login(5)
    assert client.post('/api/rules/sign-config',json={'team_id':1,'rules':[]}).status_code==403
    client=login(1)
    manifest=client.post('/api/rules/sign-config',json={'team_id':1,'rules':[
      {'name':'Approved file rule','condition':{'field':'new.custom.identifier','op':'eq','value':'X'},
       'priority':2,'presentation':{'title':'Approved {{new.custom.identifier}}','body':'from signed file'}}]})
    assert manifest.status_code==200,manifest.text
    from app.services.config_reload import apply_signed_manifest,reload_approved_rule_files
    fake={**manifest.json(),'rules':[{'name':'Sneaky','condition':{}}]}
    with Session() as db:
        try:apply_signed_manifest(db,fake)
        except ValueError:db.rollback()
        else:assert False,'tampered file was accepted'
    config_path=tmp_path/'rules.json';config_path.write_text(json.dumps(manifest.json()))
    reload_approved_rule_files(Session,tmp_path)
    with Session() as db:
        rule=db.query(Rule).filter_by(name='Approved file rule').one()
        assert rule.team_id==1 and rule.priority==2
    # A manager can sign a revised file. Changes take effect on the NEXT event.
    client=login(1)
    revised={**manifest.json()['rules'][0],'priority':3}
    signature=client.post('/api/rules/sign-config',json={'team_id':1,'rules':[revised]}).json()
    config_path.write_text(json.dumps(signature))
    reload_approved_rule_files(Session,tmp_path)
    with Session() as db:
        rule=db.query(Rule).filter_by(name='Approved file rule').one()
        assert rule.priority==3 and rule.version==2


def test_explicit_manager_only_history_purge_retains_tombstone(login):
    complete_runbook(login, 1, 2)
    client=login(2)
    assert client.post('/api/tickets/1/move',json={'state':'done'}).status_code==200
    client=login(1)
    assert client.post('/api/tickets/1/approve').status_code==200
    assert client.post('/api/tickets/1/purge',json={'confirmation':'not confirmed'}).status_code==422
    client=login(3)
    assert client.post('/api/tickets/1/purge',json={'confirmation':'PURGE TKT-00001'}).status_code==403
    client=login(1)
    assert client.post('/api/tickets/1/purge',json={'confirmation':'PURGE TKT-00001'}).status_code==200
    assert client.get('/api/tickets/1').status_code==410
    with Session() as db:
        assert db.get(Ticket,1).state=='purged'
        assert all(e.raw=='[explicitly purged]' for e in db.query(Event).filter_by(ticket_id=1))
        assert db.query(Audit).filter_by(ticket_id=1,action='ticket_purged_tombstone').count()==1
