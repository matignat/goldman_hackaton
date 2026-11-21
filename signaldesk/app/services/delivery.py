"""Durable notification jobs, schedule/buffer calculation, adapter isolation and fallback."""
import os,smtplib
from email.message import EmailMessage
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session
from ..models import Delivery,ChannelState,User,Team,Membership,Policy,now
from .records import audit

class ChannelRegistry:
    def __init__(self):self.senders={}
    def register(self,name,fn):self.senders[name]=fn
    def send(self,name,job,user):
        sender=self.senders.get(name)
        if not sender:raise ValueError('Unknown notification channel')
        return sender(job,user)

channels=ChannelRegistry()
channels.register('ui',lambda job,user:True)
channels.register('sms',lambda job,user:True) # Captured in Delivery outbox
channels.register('phone',lambda job,user:True) # Captured in Delivery outbox

def email_sender(job,user):
    host=os.getenv('SMTP_HOST')
    if not host:return True # Capture-only mode, durable outbox
    msg=EmailMessage()
    msg['Subject']=f"[SignalDesk] {job.content.get('title','Notification')}"
    msg['From']='alerts@signaldesk.local'
    msg['To']=user.email
    msg.set_content(job.content.get('body',''))
    with smtplib.SMTP(host,int(os.getenv('SMTP_PORT','1025')),timeout=5) as smtp:smtp.send_message(msg)
    return True
channels.register('email',email_sender)

RETRY_SECONDS=[30,120,600,1800,7200]
def next_window(user:User,dt:datetime)->datetime:
    """Return current instant if inside daily local window; next start otherwise."""
    window=user.schedule or {}
    if window.get('enabled') is False:return dt
    zone=ZoneInfo(window.get('timezone') or 'UTC')
    local=dt.astimezone(zone)
    try:
        h1,m1=map(int,window.get('start','00:00').split(':'))
        h2,m2=map(int,window.get('end','23:59').split(':'))
        minute=local.hour*60+local.minute
        start=h1*60+m1;end=h2*60+m2
        if start<=minute<=end:return dt
        candidate=local.replace(hour=h1,minute=m1,second=0,microsecond=0)
        if minute>end:candidate+=timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    except (ValueError,OverflowError):return dt

def queue_one(db:Session,user:User,ticket,channel,kind,summary,key,forced=False,is_primary=False,ignore_schedule=False):
    from sqlalchemy.exc import IntegrityError
    existing=db.query(Delivery).filter_by(idempotency_key=key).first()
    if existing:return existing
    current=now();scheduled=current if ignore_schedule else next_window(user,current)
    if channel!='ui':scheduled=max(scheduled,current+timedelta(seconds=max(0,user.buffer_seconds)))
    # The first job opens a fixed per-recipient/channel delivery window. Subsequent
    # independent ticket events join the same durable job; no schedule postponement.
    # The original event keys are retained inside the digest for idempotent replay.
    if channel!='ui' and user.buffer_seconds and not kind.endswith('_fallback'):
        pending=(db.query(Delivery).filter(Delivery.user_id==user.id,
                         Delivery.channel==channel,Delivery.status.in_(['queued','held']),
                         Delivery.is_primary==is_primary,Delivery.available_at>current)
                 .order_by(Delivery.available_at,Delivery.id).all())
        for job in pending:
            entries=(job.content or {}).get('entries')
            if not entries or not (job.content or {}).get('digest'):continue
            if any(entry['key']==key for entry in entries):return job
            job_time=job.available_at.replace(tzinfo=timezone.utc) if job.available_at.tzinfo is None else job.available_at
            if job_time>current+timedelta(seconds=max(0,user.buffer_seconds)+60):continue
            entries=[*entries,{'key':key,'ticket_id':ticket.id if ticket else None,
                               'kind':kind,'title':summary.get('title','')}]
            content={**job.content,'entries':entries,'count':len(entries),
                     'title':f'{len(entries)} updates across {len({x["ticket_id"] for x in entries})} tickets',
                     'body':'\n'.join(f"• #{x['ticket_id']} {x['kind']}: {x['title']}" for x in entries)}
            job.content=content
            return job
    if channel!='ui' and user.buffer_seconds:
        summary={**summary,'digest':True,'count':1,
                 'entries':[{'key':key,'ticket_id':ticket.id if ticket else None,
                             'kind':kind,'title':summary.get('title','')}]}
    job=Delivery(user_id=user.id,ticket_id=ticket.id if ticket else None,
                 channel=channel,kind=kind,status='held' if scheduled>current else 'queued',
                 idempotency_key=key,content=summary,available_at=scheduled,is_primary=is_primary)
    db.add(job)
    return job

def queue_ticket(db,ticket,kind='new_ticket',extra_users=None):
    team=db.get(Team,ticket.team_id)
    member_ids=[m.user_id for m in db.query(Membership).filter_by(team_id=ticket.team_id)]
    rule=db.get(__import__('app.models',fromlist=['Rule']).Rule,ticket.rule_id) if ticket.rule_id else None
    routing=(rule.routing or {}) if rule else {}
    recipients=routing.get('recipients','team')
    if kind=='assigned' and extra_users:
        targets=set(extra_users)
    elif recipients=='leader':targets={team.leader_id} if team else set()
    elif recipients=='maintainers':targets={m.user_id for m in db.query(Membership).filter_by(team_id=ticket.team_id,maintainer=True)}
    elif recipients=='oncall':targets={team.leader_id} if team else set() # seeded manager is on-call; configurable roster is an extension
    elif isinstance(recipients,list):targets={int(uid) for uid in recipients}
    else:targets=set(member_ids)
    if team and not (kind=='assigned' and extra_users):targets.add(team.leader_id)
    policies=db.query(Policy).filter_by(team_id=ticket.team_id).all()
    # Forced policies reach their eligible members even when not selected by a
    # particular rule's optional recipient list.
    for p in policies:
        if p.kind not in ('all',kind) or p.severity not in ('all',ticket.severity):continue
        if p.target=='all':targets.update(member_ids)
        elif p.target=='maintainers':targets.update(m.user_id for m in db.query(Membership).filter_by(team_id=ticket.team_id,maintainer=True))
        elif p.target.startswith('user:'):
            try:targets.add(int(p.target[5:]))
            except ValueError:pass
    for uid in targets:
        user=db.get(User,uid)
        if not user:continue
        blocked=(user.subscriptions or {}).get(ticket.severity) is False
        applicable=[p for p in policies if (p.severity in ('all',ticket.severity)) and p.kind in ('all',kind)
                    and (p.target=='all' and uid in member_ids or p.target=='maintainers' and
                         bool(db.get(Membership,(uid,ticket.team_id)) and db.get(Membership,(uid,ticket.team_id)).maintainer)
                         or p.target==f'user:{uid}')]
        forced={p.channel for p in applicable}
        mandatory=set(routing.get('mandatory_channels',[]))
        channels=forced|mandatory
        if not blocked:channels.add(user.primary_channel)
        summary={'title':ticket.title,'body':f'{ticket.body}\nTicket #{ticket.id}\nRunbook: /#ticket-{ticket.id}',
                 'ticket_id':ticket.id,'count':ticket.occurrences,'kind':kind}
        for channel in channels:
            if channel not in ('ui','email','sms','phone'):continue
            key=f'{ticket.id}:{kind}:{uid}:{channel}'
            queue_one(db,user,ticket,channel,kind,summary,key,forced=(channel in forced or channel in mandatory),is_primary=(channel==user.primary_channel),ignore_schedule=any(p.channel==channel and p.ignore_schedule for p in applicable))

def process_deliveries(db:Session,limit=100):
    count=0;current=now()
    q=(db.query(Delivery).filter(Delivery.status.in_(['queued','held','retrying']),Delivery.available_at<=current)
       .order_by(Delivery.available_at,Delivery.id))
    # The same job must not be submitted concurrently by two processing pods.
    if db.bind.dialect.name=='postgresql':q=q.with_for_update(skip_locked=True)
    jobs=q.limit(limit).all()
    for job in jobs:
        user=db.get(User,job.user_id)
        channel=db.get(ChannelState,job.channel)
        try:
            if channel and channel.failing:raise ConnectionError(f'{job.channel} test provider unavailable')
            channels.send(job.channel,job,user)
            job.status='sent';job.sent_at=now();job.last_error=''
        except Exception as exc:
            job.attempts+=1;job.last_error=str(exc)[:200]
            if job.attempts<=len(RETRY_SECONDS):
                job.status='retrying';job.available_at=now()+timedelta(seconds=RETRY_SECONDS[job.attempts-1])
            else:
                job.status='failed'
                if job.is_primary and user.backup_channel!=job.channel:
                    fallback_key=job.idempotency_key+':fallback'
                    fallback=queue_one(db,user,db.get(__import__('app.models',fromlist=['Ticket']).Ticket,job.ticket_id) if job.ticket_id else None,user.backup_channel,job.kind,job.content,fallback_key)
                    fallback.fallback_for=job.id
                audit(db,'delivery_failed',ticket_id=job.ticket_id,detail={'channel':job.channel,'user_id':job.user_id})
        count+=1
    return count
