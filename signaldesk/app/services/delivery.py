"""Durable notification jobs, schedule/buffer calculation, adapter isolation and fallback."""
import os,smtplib
from email.message import EmailMessage
from datetime import timedelta
from sqlalchemy.orm import Session
from ..models import Delivery,ChannelState,User,Team,Membership,Policy,now
from .records import audit

class ChannelRegistry:
    def __init__(self):
        self.senders = {}
        self.labels = {}
        self.contact_fields = {}

    def register(self, name, fn, label=None, contact=None):
        self.senders[name] = fn
        self.labels[name] = label or name.title()
        self.contact_fields[name] = contact

    def available(self, user):
        return {name: label for name, label in self.labels.items()
                if not self.contact_fields[name] or getattr(user,self.contact_fields[name],None)}
    def send(self,name,job,user):
        sender=self.senders.get(name)
        if not sender:raise ValueError('Unknown notification channel')
        return sender(job,user)

channels=ChannelRegistry()
channels.register('ui',lambda job,user:True, label='UI')
channels.register('sms',lambda job,user:True, label='SMS', contact='phone') # Captured in Delivery outbox
channels.register('phone',lambda job,user:True, label='Phone call', contact='phone') # Captured in Delivery outbox

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
channels.register('email',email_sender, contact='email')

TICKET_CATEGORIES = {'critical':'Critical incident', 'error':'Error incident', 'warning':'Warning incident',
                     'info':'Informational incident', 'task':'Task', 'change_request':'Change request'}

def ticket_channels(user):
    """Preserve existing direct routes until the employee saves their new selections."""
    saved = user.delivery_preferences.channels if user.delivery_preferences else {}
    available = channels.available(user)
    result = {}
    for category in TICKET_CATEGORIES:
        severity = category if category not in ('task','change_request') else 'info'
        previous = [user.primary_channel, *user.preferred_channels, *user.severity_channels.get(severity, [])]
        choices = saved.get(category, previous)
        result[category] = sorted(set(choices).intersection(available)) or ['ui']
    return result

RETRY_SECONDS=[30,120,600,1800,7200]
def queue_one(db:Session,user:User,ticket,channel,kind,summary,key):
    existing=db.query(Delivery).filter_by(idempotency_key=key).first()
    if existing:return existing
    job=Delivery(user_id=user.id,ticket_id=ticket.id if ticket else None,
                 channel=channel,kind=kind,status='queued',
                 idempotency_key=key,content=summary,available_at=now())
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
    category = ticket.type if ticket.type in ('task','change_request') else ticket.severity
    policies=db.query(Policy).filter_by(team_id=ticket.team_id).all()
    # Forced policies reach their eligible members even when not selected by a
    # particular rule's optional recipient list.
    for p in policies:
        if p.kind not in ('all',kind) or p.severity not in ('all',category):continue
        if p.target=='all':targets.update(member_ids)
        elif p.target=='maintainers':targets.update(m.user_id for m in db.query(Membership).filter_by(team_id=ticket.team_id,maintainer=True))
        elif p.target.startswith('user:'):
            try:targets.add(int(p.target[5:]))
            except ValueError:pass
    for uid in targets:
        user=db.get(User,uid)
        if not user:continue
        blocked=(user.subscriptions or {}).get(ticket.severity) is False
        applicable=[p for p in policies if (p.severity in ('all',category)) and p.kind in ('all',kind)
                    and (p.target=='all' and uid in member_ids or p.target=='maintainers' and
                         bool(db.get(Membership,(uid,ticket.team_id)) and db.get(Membership,(uid,ticket.team_id)).maintainer)
                         or p.target==f'user:{uid}')]
        forced={p.channel for p in applicable}
        mandatory=set(routing.get('mandatory_channels',[]))
        selected=forced|mandatory
        if not blocked:
            routes = ticket_channels(user)
            selected.update(routes.get(category, routes['info']))
        summary={'title':ticket.title,'body':f'{ticket.body}\nTicket #{ticket.id}\nRunbook: /#ticket-{ticket.id}',
                 'ticket_id':ticket.id,'count':ticket.occurrences,'kind':kind}
        for channel in selected:
            if channel not in channels.senders:continue
            key=f'{ticket.id}:{kind}:{uid}:{channel}'
            queue_one(db,user,ticket,channel,kind,summary,key)

def process_deliveries(db:Session,limit=100):
    count=0;current=now()
    q=(db.query(Delivery).filter((Delivery.status=='held') | (Delivery.status.in_(['queued','retrying']) & (Delivery.available_at<=current)))
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
                audit(db,'delivery_failed',ticket_id=job.ticket_id,detail={'channel':job.channel,'user_id':job.user_id})
        count+=1
    return count
