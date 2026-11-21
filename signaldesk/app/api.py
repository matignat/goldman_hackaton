"""SignalDesk HTTP API. All mutations check capabilities on the server."""
import os,json,asyncio,statistics,time,uuid
from datetime import datetime,timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI,HTTPException,Request,Response,Depends,Body,Header
from fastapi.responses import HTMLResponse,StreamingResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func
from .database import init_db,Session as DBSession
from .models import User,Team,Membership,Source,Rule,Runbook,Event,Ticket,Audit,Activity,Delivery,ChannelState,Proposal,BoardAccess,Policy,now
from .auth import SIGNER,db_session,current_user,require,require_role,require_debug,is_member,is_manager,is_maintainer,is_guest,can_view_board,can_view_ticket,can_work,can_approve_final
from .services.conditions import validate,matches,render,canonical
from .services.runbooks import formats,actions
from .services.ingestion import ingest
from .services.routing import pump_events,process_event
from .services.delivery import queue_ticket,process_deliveries
from .services.records import audit,activity
from .services.config_reload import sign_manifest,validate_manifest

app=FastAPI(title='SignalDesk API',version='1.0.0',docs_url='/api/docs',openapi_url='/api/openapi.json')
static=Path(__file__).parent/'static'
app.mount('/assets',StaticFiles(directory=str(static)),name='assets')

@app.on_event('startup')
def startup():init_db()

@app.get('/',response_class=HTMLResponse)
def index():return (static/'index.html').read_text()

@app.get('/api/health')
def health(db:Session=Depends(db_session)):
    db.execute(__import__('sqlalchemy').text('SELECT 1'))
    return {'ok':True,'service':'signaldesk','mode':'demo' if os.getenv('DEMO_MODE','1')=='1' else 'production'}

def serialized(obj):
    if isinstance(obj,datetime):return obj.isoformat()
    if isinstance(obj,(dict,list,tuple)):return json.loads(json.dumps(obj,default=str))
    return obj

def row(obj,keys):return {k:serialized(getattr(obj,k)) for k in keys}

def team_dict(db,t):
    leader=db.get(User,t.leader_id)
    return {'id':t.id,'name':t.name,'slug':t.slug,'leader_id':t.leader_id,'leader_name':leader.name if leader else '',
            'members':[{'id':m.user_id,'name':db.get(User,m.user_id).name,'maintainer':m.maintainer} for m in db.query(Membership).filter_by(team_id=t.id).all()]}

def ticket_dict(t,detail=False):
    keys=['id','team_id','rule_id','source_id','event_id','type','state','incident_status','title','body','severity','environment','schema','occurred_at','last_occurrence_at','created_at','updated_at','resolved_at','archived_at','occurrences','flags','members','runbook_steps','done_by','approved_by','recurrence_of']
    x=row(t,keys)
    x['display_id']=f'TKT-{t.id:05d}'
    if detail:x.update(row(t,['first_payload','latest_payload','samples','fingerprint']))
    return x

@app.get('/api/personas')
def personas(db:Session=Depends(db_session)):
    require(os.getenv('DEMO_MODE','1')=='1','Demo persona selector disabled')
    return [{'id':u.id,'name':u.name,'role':u.role,'debug':u.debug_enabled} for u in db.query(User).order_by(User.id)]

@app.post('/api/session')
def signin(data:dict=Body(...),db:Session=Depends(db_session)):
    require(os.getenv('DEMO_MODE','1')=='1','Demo sign-in disabled')
    u=db.get(User,data.get('user_id'))
    if not u:raise HTTPException(404,'Persona not found')
    response=Response(status_code=204)
    response.set_cookie('signaldesk_session',SIGNER.dumps(u.id),httponly=True,samesite='strict',secure=os.getenv('HTTPS_ONLY','0')=='1',max_age=86400)
    return response

@app.post('/api/logout')
def logout():
    resp=Response(status_code=204);resp.delete_cookie('signaldesk_session');return resp

@app.get('/api/me')
def me(u:User=Depends(current_user),db:Session=Depends(db_session)):
    myteams=[team_dict(db,db.get(Team,m.team_id)) for m in db.query(Membership).filter_by(user_id=u.id)]
    managed=[team_dict(db,t) for t in db.query(Team).filter_by(leader_id=u.id) if t.id not in {x['id'] for x in myteams}]
    guest=[team_dict(db,db.get(Team,g.team_id)) for g in db.query(BoardAccess).filter_by(user_id=u.id,status='approved') if g.team_id not in {x['id'] for x in myteams+managed}]
    return {**row(u,['id','name','role','email','phone','manager_id','primary_channel','backup_channel','subscriptions','schedule','buffer_seconds','preset','timezone','debug_enabled']),
            'teams':myteams+managed,'shared_teams':guest,'is_demo':os.getenv('DEMO_MODE','1')=='1'}

@app.get('/api/teams')
def teams(u:User=Depends(current_user),db:Session=Depends(db_session)):
    return [team_dict(db,t) for t in db.query(Team).order_by(Team.position,Team.id) if can_view_board(db,u,t.id)]

@app.get('/api/board')
def board(team:int|None=None,severity:str='',search:str='',my_tickets:bool=False,unassigned:bool=False,u:User=Depends(current_user),db:Session=Depends(db_session)):
    allowed=[t.id for t in db.query(Team) if can_view_board(db,u,t.id)]
    if team is not None:require(team in allowed);allowed=[team]
    if not allowed:return {'tickets':[],'counters':{'total':0,'new':0,'acknowledged':0,'in_progress':0,'done':0,'critical':0,'attention':0,'awaiting_approval':0},'total':0}
    all_tickets=db.query(Ticket).filter(Ticket.team_id.in_(allowed),Ticket.state.notin_(['archived','purged'])).order_by(Ticket.updated_at.desc()).all()
    visible=[t for t in all_tickets if (not severity or t.severity==severity) and (not search or search.lower() in (t.title+' '+t.body+' '+str(t.id)).lower()) and (not my_tickets or u.id in (t.members or [])) and (not unassigned or not t.members)]
    counter=lambda ts:{'total':len(ts),**{s:sum(t.state==s for t in ts) for s in ['new','acknowledged','in_progress','done']},
                        'critical':sum(t.severity=='critical' and t.type=='incident' for t in ts),
                        'attention':sum('attention' in (t.flags or []) for t in ts),
                        'awaiting_approval':sum(t.state=='done' for t in ts)}
    return {'tickets':[ticket_dict(t) for t in visible],'counters':counter(visible),'total':len(all_tickets),'all_counters':counter(all_tickets)}

@app.get('/api/tickets')
def tickets(handled:bool=False,team:int|None=None,u:User=Depends(current_user),db:Session=Depends(db_session)):
    q=db.query(Ticket)
    if team: q=q.filter_by(team_id=team)
    q=q.filter(Ticket.state=='archived' if handled else Ticket.state.notin_(['archived','purged']))
    items=q.order_by(Ticket.updated_at.desc()).limit(250).all()
    if handled:items=[t for t in items if u.id in (t.members or []) or u.id in (t.done_by,t.approved_by)]
    else:items=[t for t in items if can_view_ticket(db,u,t)]
    return [ticket_dict(t) for t in items]

@app.get('/api/tickets/{ticket_id}')
def ticket_detail(ticket_id:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404,'Ticket not found')
    if t.state=='purged':raise HTTPException(410,'Ticket has been purged; audit tombstone remains')
    require(can_view_ticket(db,u,t))
    timeline=db.query(Audit).filter_by(ticket_id=ticket_id).order_by(Audit.id.asc()).all()
    deliveries=db.query(Delivery).filter_by(ticket_id=ticket_id).order_by(Delivery.id.desc()).limit(40).all()
    rule=db.get(Rule,t.rule_id) if t.rule_id else None
    runbook=db.get(Runbook,rule.runbook_id) if rule and rule.runbook_id else None
    first_event=db.get(Event,t.event_id) if t.event_id else None
    latest_event=(db.query(Event).filter_by(ticket_id=ticket_id,state='matched').order_by(Event.id.desc()).first()
                  if t.type=='incident' else None)
    return {**ticket_dict(t,True),
            'runbook_source':runbook.source_text if runbook else None,
            'runbook_format':runbook.source_format if runbook else None,
            'first_normalized':first_event.normalized if first_event else None,
            'latest_normalized':latest_event.normalized if latest_event else None,
            'timeline':[row(a,['id','actor','action','detail','created_at']) for a in timeline],
            'deliveries':[row(d,['id','channel','user_id','kind','status','attempts','last_error']) for d in deliveries],
            'permissions':{'can_work':can_work(db,u,t) and t.state!='archived' and not is_guest(db,u,t.team_id),
                           'can_approve':can_approve_final(db,u,t) and t.done_by!=u.id,
                           'can_manage':is_manager(db,u,t.team_id),
                           'can_join':is_member(db,u,t.team_id) and u.id not in (t.members or [])},
            'members':[{'id':uid,'name':db.get(User,uid).name if db.get(User,uid) else 'Unknown'} for uid in (t.members or [])]}

@app.post('/api/tickets')
def create_task(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    team_id=int(data.get('team_id',0))
    require(is_member(db,u,team_id) or is_manager(db,u,team_id))
    title=str(data.get('title','')).strip()
    if not title:raise HTTPException(422,'Title required')
    t=Ticket(team_id=team_id,type='task',title=title[:250],body=str(data.get('body','')),severity='info',state='new',occurrences=0,
             members=[u.id],first_payload={},latest_payload={},samples=[],runbook_steps=[],flags=[],created_at=now(),updated_at=now())
    db.add(t);db.flush()
    audit(db,'task_created',team_id,t.id,u)
    activity(db,team_id,t.id,'task','Task created: '+t.title)
    queue_ticket(db,t,'new_ticket')
    db.commit()
    return ticket_dict(t)

@app.post('/api/tickets/{ticket_id}/join')
def join(ticket_id:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(is_member(db,u,t.team_id) and t.state!='archived')
    t.members=list(set((t.members or [])+[u.id]));t.updated_at=now()
    audit(db,'ticket_joined',t.team_id,t.id,u)
    activity(db,t.team_id,t.id,'ticket','Joined ticket: '+t.title)
    db.commit();return ticket_dict(t)

@app.post('/api/tickets/{ticket_id}/members')
def add_member(ticket_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(can_work(db,u,t) and t.state!='archived' and not is_guest(db,u,t.team_id))
    candidate=db.get(User,data.get('user_id'))
    if not candidate:raise HTTPException(404,'Employee not found')
    if candidate.id not in (t.members or []):
        t.members=[*(t.members or []),candidate.id]
        queue_ticket(db,t,'assigned',extra_users=[candidate.id])
        audit(db,'ticket_member_added',t.team_id,t.id,u,{'member_id':candidate.id})
    t.updated_at=now();db.commit();return ticket_dict(t)

@app.post('/api/tickets/{ticket_id}/move')
def move(ticket_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(t.type in ('incident','task') and t.state!='archived' and can_work(db,u,t) and not is_guest(db,u,t.team_id))
    dest=data.get('state')
    if dest not in ('acknowledged','in_progress','done'):raise HTTPException(422,'Invalid target state')
    if t.state=='done' and dest!='done':raise HTTPException(409,'Only rejection or relapse reopens Done')
    old=t.state;t.state=dest;t.updated_at=now()
    if u.id not in (t.members or []):t.members=[*(t.members or []),u.id]
    if dest=='done':t.done_by=u.id;queue_ticket(db,t,'approval_needed')
    audit(db,'ticket_moved',t.team_id,t.id,u,{'from':old,'to':dest,'note':str(data.get('note',''))[:400]})
    activity(db,t.team_id,t.id,'ticket',f'{t.title}: {old.replace("_"," ")} → {dest.replace("_"," ")}')
    db.commit();return ticket_dict(t)

@app.post('/api/tickets/{ticket_id}/approve')
def approve(ticket_id:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(t.type in ('task','incident') and t.state=='done' and can_approve_final(db,u,t),'Manager/maintainer approval required')
    require(t.done_by!=u.id,'Separation of duties: the person marking Done cannot approve')
    t.state='archived';t.active_key=None;t.archived_at=now();t.updated_at=now();t.approved_by=u.id
    audit(db,'ticket_approved',t.team_id,t.id,u)
    activity(db,t.team_id,t.id,'approval','Approved and archived: '+t.title)
    queue_ticket(db,t,'resolve_approved')
    db.commit();return ticket_dict(t)

@app.post('/api/tickets/{ticket_id}/reject')
def reject(ticket_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(t.type in ('incident','task') and t.state=='done' and can_approve_final(db,u,t))
    comment=str(data.get('comment','')).strip()
    if not comment:raise HTTPException(422,'Reason is required')
    t.state='in_progress';t.done_by=None;t.updated_at=now()
    audit(db,'ticket_rejected',t.team_id,t.id,u,{'comment':comment})
    activity(db,t.team_id,t.id,'approval','Returned to In progress: '+t.title)
    queue_ticket(db,t,'resolve_rejected')
    db.commit();return ticket_dict(t)

@app.post('/api/tickets/{ticket_id}/comment')
def comment(ticket_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(can_view_ticket(db,u,t) and t.state!='archived' and not is_guest(db,u,t.team_id))
    text=str(data.get('comment','')).strip()
    if not text:raise HTTPException(422,'Comment is required')
    audit(db,'comment',t.team_id,t.id,u,{'text':text[:4000]})
    activity(db,t.team_id,t.id,'comment',u.name+' commented on '+t.title)
    t.updated_at=now();db.commit();return {'ok':True}

@app.post('/api/tickets/{ticket_id}/attention')
def acknowledge_attention(ticket_id:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(can_work(db,u,t) and t.state!='archived');t.flags=[v for v in (t.flags or []) if v!='attention']
    audit(db,'attention_acknowledged',t.team_id,t.id,u);db.commit();return {'ok':True}

@app.post('/api/tickets/{ticket_id}/steps/{step_idx}/run')
def run_step(ticket_id:int,step_idx:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(can_work(db,u,t) and t.state!='archived' and not is_guest(db,u,t.team_id))
    steps=list(t.runbook_steps or [])
    if not 0<=step_idx<len(steps):raise HTTPException(404,'Step not found')
    step={**steps[step_idx]}
    if step.get('status')=='done':raise HTTPException(409,'Step already complete')
    if step.get('approval')=='required' and not step.get('approved_by'):
        step['status']='waiting_approval';steps[step_idx]=step;t.runbook_steps=steps
        t.flags=list(set((t.flags or [])+['action_approval']))
        audit(db,'action_approval_requested',t.team_id,t.id,u,{'step':step_idx})
        queue_ticket(db,t,'action_approval_needed');db.commit()
        return ticket_dict(t)
    try:
        outcome=actions.execute(step['action'],step.get('params',{}),t,db) if step.get('type')=='action' else {'status':'completed_manual'}
        step.update(status='done',done_by=u.id,outcome=outcome)
    except Exception as exc:step.update(status='failed',outcome={'error':str(exc)[:100]})
    steps[step_idx]=step;t.runbook_steps=steps;t.updated_at=now()
    audit(db,'runbook_step',t.team_id,t.id,u,{'step':step_idx,'result':step['status']})
    activity(db,t.team_id,t.id,'runbook','Runbook step: '+t.title)
    db.commit();return ticket_dict(t)

@app.post('/api/tickets/{ticket_id}/steps/{step_idx}/approve')
def approve_step(ticket_id:int,step_idx:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404)
    require(is_manager(db,u,t.team_id),'Only designated team manager approves risky actions')
    steps=list(t.runbook_steps or [])
    if not 0<=step_idx<len(steps):raise HTTPException(404)
    step={**steps[step_idx]}
    if step.get('status')!='waiting_approval':raise HTTPException(409,'No pending step approval')
    step['approved_by']=u.id;step['status']='pending';steps[step_idx]=step;t.runbook_steps=steps
    if not any(s.get('status')=='waiting_approval' for s in steps):t.flags=[f for f in (t.flags or []) if f!='action_approval']
    audit(db,'runbook_action_approved',t.team_id,t.id,u,{'step':step_idx})
    db.commit();return ticket_dict(t)


@app.post('/api/tickets/{ticket_id}/purge')
def purge_ticket(ticket_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404,'Ticket not found')
    require(is_manager(db,u,t.team_id),'Only the designated team manager can purge history')
    if t.state!='archived':raise HTTPException(409,'Only approved archived tickets can be purged')
    if str(data.get('confirmation',''))!=f'PURGE TKT-{t.id:05d}':
        raise HTTPException(422,f'Type PURGE TKT-{t.id:05d} to confirm')
    # This is an explicit destructive operation: remove raw linked event data,
    # delivery payloads and prior comment bodies, retaining the audited tombstone.
    for ev in db.query(Event).filter_by(ticket_id=t.id):
        ev.raw='[explicitly purged]';ev.normalized={};ev.route_trace=[];ev.state='purged'
    for job in db.query(Delivery).filter_by(ticket_id=t.id):job.content={'redacted':True}
    for previous in db.query(Audit).filter_by(ticket_id=t.id):previous.detail={}
    for item in db.query(Activity).filter_by(ticket_id=t.id):item.label=f'Purged ticket #{t.id}'
    t.state='purged';t.type='purged';t.title='[purged]';t.body='';t.first_payload={};t.latest_payload={}
    t.samples=[];t.members=[];t.runbook_steps=[];t.flags=['purged'];t.fingerprint='';t.active_key=None
    audit(db,'ticket_purged_tombstone',t.team_id,t.id,u,{'ticket_id':t.id,'explicit':True})
    db.commit()
    return {'ok':True,'id':ticket_id,'tombstone':'ticket_purged_tombstone'}

@app.get('/api/activity')
def recent(team:int|None=None,limit:int=40,u:User=Depends(current_user),db:Session=Depends(db_session)):
    ids=[t.id for t in db.query(Team) if can_view_board(db,u,t.id)]
    if team:require(team in ids);ids=[team]
    if not ids:return []
    q=db.query(Activity).filter(Activity.team_id.in_(ids))
    return [row(a,['id','team_id','ticket_id','kind','label','count','updated_at']) for a in q.order_by(Activity.updated_at.desc(),Activity.id.desc()).limit(min(200,max(1,limit))).all()]

@app.get('/api/stream')
async def stream(team:int|None=None,u:User=Depends(current_user)):
    uid=u.id
    async def feed():
        last=None
        yield 'retry: 1500\n\n'
        while True:
            with DBSession() as db:
                user=db.get(User,uid)
                ids=[t.id for t in db.query(Team) if can_view_board(db,user,t.id)]
                if team is not None:ids=[team] if team in ids else []
                if ids:
                    a=db.query(func.max(Activity.updated_at),func.max(Activity.id)).filter(Activity.team_id.in_(ids)).first()
                    t=db.query(func.max(Ticket.updated_at)).filter(Ticket.team_id.in_(ids)).scalar()
                    d=db.query(func.max(Delivery.sent_at)).join(Ticket,Delivery.ticket_id==Ticket.id).filter(Ticket.team_id.in_(ids)).scalar()
                    signature=str((a,t,d))
                else:signature='none'
            if signature!=last:
                yield 'event: invalidate\ndata: '+json.dumps({'changed':True})+'\n\n'
                last=signature
            else:yield ': heartbeat\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(feed(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.get('/api/archive')
def archive(team:int|None=None,include_errors:bool=False,search:str='',u:User=Depends(current_user),db:Session=Depends(db_session)):
    managed=[t.id for t in db.query(Team) if is_manager(db,u,t.id)]
    require(bool(managed),'Major Incident Archive is manager-only')
    if team:require(team in managed);managed=[team]
    items=db.query(Ticket).filter(Ticket.team_id.in_(managed),Ticket.state=='archived',Ticket.type=='incident',Ticket.severity.in_(['critical','error'] if include_errors else ['critical'])).order_by(Ticket.archived_at.desc()).limit(300)
    return [ticket_dict(t) for t in items if not search or search.lower() in (t.title+' '+str(t.id)).lower()]

@app.get('/api/unmatched')
def unmatched(team:int|None=None,u:User=Depends(current_user),db:Session=Depends(db_session)):
    if u.role!='A' and u.role!='M' and not u.debug_enabled:raise HTTPException(403)
    items=db.query(Event).filter_by(state='unmatched').order_by(Event.id.desc()).limit(100).all()
    def visible(ev):
        if u.role=='A':return True
        attempted=[r.get('team_id') for r in (ev.route_trace or [])]
        return any(is_manager(db,u,t) for t in attempted) if u.role=='M' else ev.source_id==db.query(Source).filter_by(slug='demo').first().id
    return [row(e,['id','source_id','raw','received_at','error','route_trace','state']) for e in items if visible(e) and (not team or any(t.get('team_id')==team for t in e.route_trace or []))]

@app.get('/api/rules')
def rules(team:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    require(can_view_board(db,u,team))
    return [row(r,['id','team_id','name','description','priority','enabled','condition','presentation','routing','dedup_template','group_template','throttle_seconds','runbook_id','version']) for r in db.query(Rule).filter_by(team_id=team).order_by(Rule.priority,Rule.id)]

def rule_data(data):
    condition=data.get('condition') or {};validate(condition)
    presentation=data.get('presentation') or {}
    if not isinstance(presentation,dict):raise HTTPException(422,'presentation must be an object')
    routing=data.get('routing') or {}
    return {'name':str(data.get('name','Untitled rule'))[:120],'description':str(data.get('description','')),
            'priority':int(data.get('priority',100)),'enabled':bool(data.get('enabled',True)),'condition':condition,
            'presentation':presentation,'routing':routing,'dedup_template':str(data.get('dedup_template',''))[:450],
            'group_template':str(data.get('group_template',''))[:450],
            'throttle_seconds':max(0,int(data.get('throttle_seconds',0))),
            'runbook_id':data.get('runbook_id')}

@app.post('/api/rules/test')
def test_rule(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    team=int(data.get('team_id',0));require(can_view_board(db,u,team))
    rule=rule_data(data.get('rule') or {})
    payload=data.get('event')
    if payload is None:raise HTTPException(422,'Sample event required')
    norm=canonical(payload,{},now());hit=matches(rule['condition'],norm['flat'])
    return {'matched':hit,'severity':norm['severity'],'fields':norm['flat'],
            'title':render(rule['presentation'].get('title',''),norm['flat']),
            'body':render(rule['presentation'].get('body',''),norm['flat'])}

@app.post('/api/rules/sign-config')
def sign_rule_config(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    # A normal manager session is required; an A or D session cannot authorize
    # file reloads. The HMAC covers the entire rule payload and publication mode.
    team_id=int(data.get('team_id',0));require(is_manager(db,u,team_id))
    manifest={'team_id':team_id,'manager_id':u.id,'rules':data.get('rules',[]),
              'disable_missing':bool(data.get('disable_missing',False)),
              'issued_at':datetime.now(timezone.utc).isoformat()}
    candidate=sign_manifest(manifest)
    try:validate_manifest(db,candidate)
    except (ValueError,PermissionError,TypeError) as exc:raise HTTPException(422,str(exc))
    audit(db,'rule_manifest_authorized',team_id,actor=u,detail={'rules':len(manifest['rules'])})
    db.commit()
    return candidate

@app.post('/api/rules')
def create_rule(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    tid=int(data.get('team_id',0));require(is_manager(db,u,tid),'Team manager required to publish a rule')
    try:payload=rule_data(data)
    except ValueError as e:raise HTTPException(422,str(e))
    r=Rule(team_id=tid,**payload);db.add(r);db.flush()
    audit(db,'rule_published',tid,actor=u,detail={'rule_id':r.id});db.commit();return {'id':r.id,'version':r.version}

@app.put('/api/rules/{rule_id}')
def edit_rule(rule_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    r=db.get(Rule,rule_id)
    if not r:raise HTTPException(404)
    require(is_manager(db,u,r.team_id))
    try:payload=rule_data(data)
    except ValueError as e:raise HTTPException(422,str(e))
    for k,v in payload.items():setattr(r,k,v)
    r.version+=1;audit(db,'rule_updated',r.team_id,actor=u,detail={'rule_id':r.id,'version':r.version});db.commit();return {'ok':True}

@app.delete('/api/rules/{rule_id}')
def delete_rule(rule_id:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    r=db.get(Rule,rule_id)
    if not r:raise HTTPException(404)
    require(is_manager(db,u,r.team_id));r.enabled=False
    audit(db,'rule_disabled',r.team_id,actor=u,detail={'rule_id':r.id});db.commit();return {'ok':True,'note':'Soft-disabled for history safety'}

@app.get('/api/runbooks')
def runbooks(team:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    require(can_view_board(db,u,team))
    return [row(r,['id','team_id','name','source_format','source_text','steps','version']) for r in db.query(Runbook).filter_by(team_id=team)]

@app.post('/api/runbooks')
def create_runbook(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    tid=int(data.get('team_id',0));require(is_manager(db,u,tid))
    raw=str(data.get('source_text',''))
    fmt=str(data.get('source_format','markdown'))
    if not raw:raise HTTPException(422,'Runbook content required')
    steps=formats.parse(fmt,raw)
    r=Runbook(team_id=tid,name=str(data.get('name','Runbook'))[:120],source_format=fmt,source_text=raw,steps=steps)
    db.add(r);db.flush();audit(db,'runbook_published',tid,actor=u,detail={'runbook_id':r.id});db.commit()
    return row(r,['id','steps','version'])

@app.put('/api/runbooks/{rb_id}')
def edit_runbook(rb_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    rb=db.get(Runbook,rb_id)
    if not rb:raise HTTPException(404)
    require(is_manager(db,u,rb.team_id))
    rb.name=str(data.get('name',rb.name))
    rb.source_format=str(data.get('source_format',rb.source_format))
    rb.source_text=str(data.get('source_text',rb.source_text))
    rb.steps=formats.parse(rb.source_format,rb.source_text);rb.version+=1
    audit(db,'runbook_updated',rb.team_id,actor=u,detail={'runbook_id':rb.id});db.commit();return {'id':rb.id,'steps':rb.steps}

@app.get('/api/proposals')
def proposals(team:int|None=None,u:User=Depends(current_user),db:Session=Depends(db_session)):
    q=db.query(Proposal)
    if team:q=q.filter_by(team_id=team)
    items=q.order_by(Proposal.created_at.desc()).limit(150).all()
    return [row(p,['id','team_id','requester_id','target_type','target_id','description','patch','status','comment','revised_from','ticket_id','created_at']) for p in items if p.requester_id==u.id or is_manager(db,u,p.team_id) or is_maintainer(db,u,p.team_id)]

@app.post('/api/proposals')
def create_proposal(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    tid=int(data.get('team_id',0));require(is_member(db,u,tid) or is_manager(db,u,tid))
    target=str(data.get('target_type','rule'))
    if target not in ('rule','runbook','ticket'):raise HTTPException(422,'Unknown proposal target')
    target_id=data.get('target_id')
    existing=db.query(Proposal).filter_by(team_id=tid,requester_id=u.id,target_type=target,target_id=target_id,status='open').first()
    if existing:raise HTTPException(409,'You already have a pending request for this target')
    desc=str(data.get('description','')).strip()
    if not desc:raise HTTPException(422,'Please explain the proposed change')
    previous=db.query(Proposal).filter_by(team_id=tid,requester_id=u.id,target_type=target,target_id=target_id,status='rejected').order_by(Proposal.id.desc()).first()
    p=Proposal(team_id=tid,requester_id=u.id,target_type=target,target_id=target_id,description=desc,patch=data.get('patch') or {},revised_from=previous.id if previous else None)
    db.add(p);db.flush()
    t=Ticket(team_id=tid,type='change_request',state='new',title='CHANGE · '+target,body=desc,severity='info',members=[u.id],occurrences=0,created_at=now(),updated_at=now())
    db.add(t);db.flush();p.ticket_id=t.id
    audit(db,'change_requested',tid,t.id,u,{'proposal_id':p.id})
    activity(db,tid,t.id,'change','Change requested: '+desc[:90]);queue_ticket(db,t,'approval_needed')
    db.commit();return {'id':p.id,'ticket_id':t.id}

@app.post('/api/proposals/{proposal_id}/decision')
def decide_proposal(proposal_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    p=db.get(Proposal,proposal_id)
    if not p:raise HTTPException(404)
    require(is_manager(db,u,p.team_id),'Only team manager can decide proposals')
    if p.status!='open':raise HTTPException(409,'Change request already reviewed')
    decision=data.get('decision');require(decision in ('accepted','rejected','implemented'),'Invalid decision')
    if decision=='rejected' and not str(data.get('comment','')).strip():raise HTTPException(422,'Rejection requires reason')
    p.status=decision;p.comment=str(data.get('comment',''))
    t=db.get(Ticket,p.ticket_id)
    if t:
        t.state='archived';t.archived_at=now();t.approved_by=u.id;t.updated_at=now()
    audit(db,'change_'+decision,p.team_id,p.ticket_id,u,{'proposal_id':p.id,'comment':p.comment})
    activity(db,p.team_id,p.ticket_id,'change',f'Change request {decision}: {p.description[:80]}')
    db.commit();return {'id':p.id,'status':p.status}

@app.get('/api/delivery')
def delivery_logs(team:int|None=None,u:User=Depends(current_user),db:Session=Depends(db_session)):
    q=db.query(Delivery).order_by(Delivery.id.desc()).limit(120)
    jobs=q.all()
    return [row(d,['id','ticket_id','user_id','channel','kind','status','content','attempts','available_at','sent_at','last_error','fallback_for']) for d in jobs if d.user_id==u.id or (d.ticket_id and (t:=db.get(Ticket,d.ticket_id)) and is_manager(db,u,t.team_id) and (team is None or t.team_id==team))]

@app.put('/api/preferences')
def preferences(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    primary=data.get('primary_channel',u.primary_channel)
    backup=data.get('backup_channel',u.backup_channel)
    available={'email','sms','phone','ui'}
    if primary not in available or backup not in available or primary==backup:raise HTTPException(422,'Select two distinct active channels')
    if 'email' in (primary,backup) and not u.email:raise HTTPException(422,'Email address missing')
    if 'sms' in (primary,backup) or 'phone' in (primary,backup):
        if not u.phone:raise HTTPException(422,'Phone number missing')
    u.primary_channel=primary;u.backup_channel=backup
    if 'schedule' in data:
        sch=data['schedule']
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(sch.get('timezone','UTC')))
            for field in ('start','end'):
                datetime.strptime(sch.get(field,'00:00'),'%H:%M')
        except Exception:raise HTTPException(422,'Invalid time or timezone')
        u.schedule=sch
    if 'subscriptions' in data:u.subscriptions=data['subscriptions']
    if 'buffer_seconds' in data:u.buffer_seconds=min(3600,max(0,int(data['buffer_seconds'])))
    if 'timezone' in data:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(data['timezone']))
        except Exception:raise HTTPException(422,'Invalid timezone')
        u.timezone=str(data['timezone'])
    if 'preset' in data:
        if data['preset'] not in ('engineer','oncall','nonengineer'):raise HTTPException(422,'Unknown preset')
        u.preset=data['preset']
    audit(db,'preferences_updated',actor=u)
    db.commit();return {'ok':True}

@app.get('/api/policies')
def policies(team:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    require(can_view_board(db,u,team))
    return [row(p,['id','team_id','severity','channel','kind','target','ignore_schedule']) for p in db.query(Policy).filter_by(team_id=team)]

@app.post('/api/policies')
def create_policy(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    tid=int(data.get('team_id',0));require(is_manager(db,u,tid))
    channel=data.get('channel')
    if channel not in ('ui','email','sms','phone'):raise HTTPException(422,'Invalid channel')
    p=Policy(team_id=tid,severity=str(data.get('severity','critical')),channel=channel,kind=str(data.get('kind','new_ticket')),target=str(data.get('target','all')),ignore_schedule=bool(data.get('ignore_schedule',False)))
    db.add(p);audit(db,'policy_created',tid,actor=u);db.commit();return {'ok':True}

@app.get('/api/organization')
def organization(u:User=Depends(current_user),db:Session=Depends(db_session)):
    boss=db.get(User,u.manager_id) if u.manager_id else None
    reports=db.query(User).filter_by(manager_id=u.id).all()
    def summary(x):return {'id':x.id,'name':x.name,'role':x.role,'email':x.email,'team_ids':[m.team_id for m in db.query(Membership).filter_by(user_id=x.id)],'direct_reports':db.query(User).filter_by(manager_id=x.id).count()}
    return {'manager':summary(boss) if boss else None,'reports':[summary(e) for e in reports]}

@app.post('/api/teams/{team_id}/members')
def member_manage(team_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    require(is_manager(db,u,team_id))
    user_id=int(data.get('user_id',0));target=db.get(User,user_id)
    if not target:raise HTTPException(404)
    action=data.get('action','add')
    m=db.get(Membership,(user_id,team_id))
    if action=='add' and not m:db.add(Membership(user_id=user_id,team_id=team_id,maintainer=False))
    elif action=='remove' and m:db.delete(m)
    elif action in ('grant_maintainer','revoke_maintainer') and m:m.maintainer=action=='grant_maintainer'
    else:raise HTTPException(409,'Invalid membership change')
    audit(db,'membership_'+action,team_id,actor=u,detail={'employee':user_id});db.commit();return {'ok':True}

@app.get('/api/access')
def access_requests(u:User=Depends(current_user),db:Session=Depends(db_session)):
    reqs=db.query(BoardAccess).order_by(BoardAccess.id.desc()).all()
    return [row(a,['id','team_id','user_id','reason','status']) for a in reqs if a.user_id==u.id or is_manager(db,u,a.team_id)]

@app.post('/api/access')
def access_request(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    tid=int(data.get('team_id',0));require(db.get(Team,tid) is not None)
    require(not can_view_board(db,u,tid),'Already have board access')
    current=db.query(BoardAccess).filter_by(user_id=u.id,team_id=tid).first()
    if current and current.status=='pending':raise HTTPException(409,'Already pending')
    if current:current.status='pending';current.reason=str(data.get('reason',''))
    else:db.add(BoardAccess(user_id=u.id,team_id=tid,reason=str(data.get('reason',''))))
    audit(db,'board_access_requested',tid,actor=u);db.commit();return {'ok':True}

@app.post('/api/access/{access_id}/decision')
def access_decision(access_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    a=db.get(BoardAccess,access_id)
    if not a:raise HTTPException(404)
    require(is_manager(db,u,a.team_id))
    a.status='approved' if data.get('approved') else 'denied'
    audit(db,'board_access_'+a.status,a.team_id,actor=u,detail={'requester':a.user_id});db.commit();return {'ok':True}

@app.get('/api/admin')
def admin(u:User=Depends(current_user),db:Session=Depends(db_session)):
    require_role(u,'A')
    def source_info(s):
        record=row(s,['id','name','slug','type','routing_chain','enabled','configuration'])
        record['configuration']={k:('[redacted]' if any(secret in k.lower() for secret in ('password','secret','token','key')) else v)
                                 for k,v in (record['configuration'] or {}).items()}
        return record
    return {'sources':[source_info(s) for s in db.query(Source)],
            'teams':[team_dict(db,t) for t in db.query(Team)],
            'metrics':{'events':db.query(Event).count(),'matched':db.query(Event).filter_by(state='matched').count(),
                       'unmatched':db.query(Event).filter_by(state='unmatched').count(),'pending':db.query(Event).filter_by(state='pending').count(),
                       'rejected':db.query(Event).filter_by(state='rejected').count(),'tickets':db.query(Ticket).count(),
                       'deliveries_retrying':db.query(Delivery).filter_by(status='retrying').count()}}

@app.post('/api/admin/sources')
def admin_source(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    require_role(u,'A')
    chain=data.get('routing_chain') or [t.id for t in db.query(Team).order_by(Team.position)]
    if not isinstance(chain,list) or any(db.get(Team,int(t)) is None for t in chain):raise HTTPException(422,'Unknown team in routing chain')
    stype=data.get('type','webhook')
    if stype not in ('webhook','broker','log'):raise HTTPException(422,'Unknown source adapter')
    s=Source(name=str(data['name']),slug=str(data['slug']),type=stype,routing_chain=chain,configuration=data.get('configuration') or {})
    db.add(s);db.flush();audit(db,'source_created',actor=u,detail={'source_id':s.id});db.commit();return {'id':s.id}

@app.put('/api/admin/sources/{source_id}')
def admin_source_update(source_id:int,data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    require_role(u,'A')
    s=db.get(Source,source_id)
    if not s:raise HTTPException(404)
    for key in ('routing_chain','configuration','enabled','name'):
        if key in data:setattr(s,key,data[key])
    audit(db,'source_updated',actor=u,detail={'source_id':s.id});db.commit();return {'ok':True}

@app.post('/api/ingest/{slug}')
async def ingest_webhook(slug:str,request:Request,x_event_id:str|None=Header(default=None),x_webhook_key:str|None=Header(default=None),db:Session=Depends(db_session)):
    source=db.query(Source).filter_by(slug=slug).first()
    if not source or source.type not in ('webhook','log') or not source.enabled:raise HTTPException(404,'Unknown webhook')
    secret=(source.configuration or {}).get('webhook_key')
    if secret and secret!=x_webhook_key:raise HTTPException(401,'Invalid webhook key')
    raw=(await request.body()).decode('utf-8','replace')
    try:ev=ingest(db,source,raw,x_event_id);db.commit()
    except ValueError as e:raise HTTPException(413,str(e))
    if os.getenv('DEMO_INLINE_WORKER','0')=='1':
        process_event(db,ev);process_deliveries(db);db.commit()
    return {'event_id':ev.id,'status':'accepted'}

@app.get('/api/debug')
def debug_info(u:User=Depends(current_user),db:Session=Depends(db_session)):
    require_debug(u)
    return {'channels':[row(c,['name','failing']) for c in db.query(ChannelState)],
            'events':db.query(Event).count(),'pending':db.query(Event).filter_by(state='pending').count(),
            'unmatched':db.query(Event).filter_by(state='unmatched').count()}

@app.post('/api/debug/channel')
def debug_channel(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    require_debug(u)
    channel=db.get(ChannelState,str(data.get('name','')))
    if not channel:raise HTTPException(404)
    channel.failing=bool(data.get('failing',False));audit(db,'debug_channel_failure_toggle',actor=u,detail={'channel':channel.name,'failing':channel.failing})
    db.commit();return {'ok':True,'failing':channel.failing}

@app.post('/api/debug/generate')
def debug_generate(data:dict=Body(...),u:User=Depends(current_user),db:Session=Depends(db_session)):
    require_debug(u)
    s=db.query(Source).filter_by(slug='demo').first()
    if not s:raise HTTPException(503,'Demo source not configured')
    payload=data.get('event',{'alertname':'HighErrorRate','labels':{'service':'payments-api','severity':'critical'},'status':'firing','annotations':{'summary':'Demo error rate high'}})
    count=min(1000,max(1,int(data.get('count',1))))
    raw=json.dumps(payload)
    for i in range(count):ingest(db,s,raw)
    db.commit()
    if os.getenv('DEMO_INLINE_WORKER','0')=='1':
        pump_events(db,limit=count);process_deliveries(db);db.commit()
    return {'accepted':count,'source_id':s.id,'note':'Events queued in durable ingestion table'}

@app.get('/api/debug/events')
def debug_events(u:User=Depends(current_user),db:Session=Depends(db_session)):
    require_debug(u)
    return [row(e,['id','source_id','received_at','processed_at','state','ticket_id','route_trace','error']) for e in db.query(Event).order_by(Event.id.desc()).limit(70)]
