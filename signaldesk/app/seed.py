"""Idempotent judge-ready fixture data matching Appendices A and B."""
import json
from .database import init_db,session_scope
from .models import User,Team,Membership,Source,Rule,Runbook,ChannelState,Policy,Ticket,now
from .services.runbooks import formats
from .services.ingestion import ingest
from .services.routing import pump_events
from .services.delivery import channels,process_deliveries
from .services.records import audit,activity

WORKFLOW={"type":"WORKFLOW_FAILED","workflowId":"WF-20261023-00042","workflowName":"order-settlement","step":"validate-counterparty","severity":"ERROR","environment":"prod","timestamp":"2026-10-23T09:14:02.331Z","error":{"code":"TIMEOUT","message":"Counterparty service did not respond in 5000 ms"}}
PAYMENTS={"alertname":"HighErrorRate","labels":{"service":"payments-api","region":"eu-west","severity":"critical"},"annotations":{"summary":"Error rate 12% over the last 5 minutes"},"startsAt":"2026-10-23T09:13:00Z","status":"firing"}
TRADE={"meta":{"schema":"trade.rejected.v2","producer":"trade-gateway","ts":1792833245000},"payload":{"tradeId":"T-998877","desk":"rates","notional":25000000,"currency":"EUR","reason":"LIMIT_BREACH"}}

SETTLEMENT_YAML='''runbook: settlement-timeout
matches:
  type: WORKFLOW_FAILED
  "error.code": TIMEOUT
steps:
  - check: "Is counterparty-service healthy?"
    action: http_get
    url: "http://counterparty-service/health"
  - text: "Retry workflow {{workflowId}} after confirming dependencies"
    action: retry_workflow
    workflowId: "{{workflowId}}"
    approval: none
  - text: "Escalate if retry fails twice"
    if: "retry fails twice"
    action: escalate
    team: payments
    approval: required
'''
PAYMENTS_MD='''# HighErrorRate on payments-api
1. Open the payments dashboard and check if errors are in one region only.
2. If one region: fail traffic over to the other region (needs approval from the team lead).
3. If all regions: check the last deployment and roll it back.
4. Post an update in the #payments-incidents channel every 15 minutes.
'''
RATES_TEXT='Review {{payload.tradeId}} on desk {{payload.desk}}, confirm notional {{payload.notional}}, and contact the rates on-call desk.'

def seed():
    init_db()
    with session_scope() as db:
        if db.query(User).first():
            for team in db.query(Team).filter(~db.query(Policy.id).filter(Policy.team_id==Team.id).exists()):
                db.add(Policy(team_id=team.id,severity='critical',channel='ui'))
            return
        users=[
            User(name='Morgan Blake',role='M',email='morgan@signaldesk.local',phone='+15550001001',primary_channel='ui',backup_channel='email',preset='engineer'),
            User(name='Avery Chen',role='E',email='avery@signaldesk.local',phone='+15550001002',primary_channel='ui',backup_channel='email',preset='engineer'),
            User(name='Nora Shah',role='E',email='nora@signaldesk.local',phone='+15550001003',primary_channel='email',backup_channel='ui',preset='oncall'),
            User(name='Olivia Reed',role='M',email='olivia@signaldesk.local',phone='+15550001004',primary_channel='ui',backup_channel='email',preset='engineer'),
            User(name='Adrian Cole',role='A',email='adrian@signaldesk.local',phone='+15550001005',primary_channel='ui',backup_channel='email'),
            User(name='Dana Brooks',role='E',email='dana@signaldesk.local',phone='+15550001006',primary_channel='ui',backup_channel='email',debug_enabled=True),
            User(name='Sofia Ruiz',role='E',email='sofia@signaldesk.local',phone='+15550001007',primary_channel='ui',backup_channel='email',preset='nonengineer'),
        ]
        db.add_all(users);db.flush()
        morgan,avery,nora,olivia,adrian,dana,sofia=users
        avery.manager_id=morgan.id;nora.manager_id=morgan.id;sofia.manager_id=olivia.id
        teams=[Team(name='Payments & Reliability',slug='payments',leader_id=morgan.id,position=10),
               Team(name='Settlement Operations',slug='settlements',leader_id=morgan.id,position=20),
               Team(name='Rates Trading',slug='rates',leader_id=olivia.id,position=30),
               Team(name='Pricing Infrastructure',slug='pricing',leader_id=olivia.id,position=40)]
        db.add_all(teams);db.flush();payments,settlements,rates,pricing=teams
        db.add_all([Membership(user_id=morgan.id,team_id=payments.id),Membership(user_id=morgan.id,team_id=settlements.id),
                    Membership(user_id=avery.id,team_id=payments.id),Membership(user_id=avery.id,team_id=settlements.id),
                    Membership(user_id=nora.id,team_id=payments.id,maintainer=True),Membership(user_id=nora.id,team_id=settlements.id,maintainer=True),
                    Membership(user_id=olivia.id,team_id=rates.id),Membership(user_id=olivia.id,team_id=pricing.id),
                    Membership(user_id=sofia.id,team_id=rates.id),Membership(user_id=sofia.id,team_id=pricing.id)])
        for channel in channels.senders:db.add(ChannelState(name=channel,failing=False))
        db.add_all(Policy(team_id=team.id,severity='critical',channel='ui') for team in teams)
        rb_pay=Runbook(team_id=payments.id,name='High error rate · payments',source_format='markdown',source_text=PAYMENTS_MD,steps=formats.parse('markdown',PAYMENTS_MD))
        rb_set=Runbook(team_id=settlements.id,name='Settlement timeout recovery',source_format='yaml',source_text=SETTLEMENT_YAML,steps=formats.parse('yaml',SETTLEMENT_YAML))
        rb_rates=Runbook(team_id=rates.id,name='Trade rejection',source_format='text',source_text=RATES_TEXT,steps=formats.parse('text',RATES_TEXT))
        rb_pricing=Runbook(team_id=pricing.id,name='Pricing latency',source_format='text',source_text='Check cache health, restart the affected instance in a controlled window, then contact pricing on-call.',steps=formats.parse('text','Check cache health, restart the affected instance in a controlled window, then contact pricing on-call.'))
        db.add_all([rb_pay,rb_set,rb_rates,rb_pricing]);db.flush()
        db.add_all([
            Rule(team_id=payments.id,name='Critical payments error rate',priority=10,enabled=True,
                 condition={'all':[{'field':'alertname','op':'eq','value':'HighErrorRate'},{'field':'labels.service','op':'eq','value':'payments-api'}]},
                 presentation={'title':'High error rate · {{labels.service}}','body':'{{annotations.summary}} · Region {{labels.region}}'},
                 routing={'recipients':'team','mandatory_channels':[]},dedup_template='{{alertname}}:{{labels.service}}:{{labels.region}}',runbook_id=rb_pay.id),
            Rule(team_id=settlements.id,name='Production settlement failures',priority=10,enabled=True,
                 condition={'all':[{'field':'type','op':'eq','value':'WORKFLOW_FAILED'},{'field':'workflowName','op':'eq','value':'order-settlement'},{'field':'environment','op':'eq','value':'prod'}]},
                 presentation={'title':'Settlement failure · {{workflowId}}','body':'{{error.message}} · Step: {{step}}'},
                 routing={'recipients':'team','mandatory_channels':['ui']},dedup_template='{{workflowId}}:{{error.code}}',runbook_id=rb_set.id),
            Rule(team_id=rates.id,name='Large rejected trades',priority=10,enabled=True,
                 condition={'all':[{'field':'meta.schema','op':'wildcard','value':'trade.rejected.*'},{'field':'payload.desk','op':'eq','value':'rates'},{'field':'payload.notional','op':'gt','value':10000000}]},
                 presentation={'title':'Rejected {{payload.currency}} {{payload.notional}} — {{payload.reason}}','body':'Trade {{payload.tradeId}} · {{payload.desk}} desk'},
                 routing={'recipients':'team','mandatory_channels':['email']},dedup_template='{{payload.tradeId}}:{{payload.reason}}',runbook_id=rb_rates.id),
            Rule(team_id=pricing.id,name='Pricing WARN log events',priority=10,enabled=True,
                 condition={'all':[{'field':'canonical.service','op':'wildcard','value':'pricing-engine*'},{'field':'canonical.severity','op':'in','value':['warning','error','critical']}]},
                 presentation={'title':'Pricing alert · {{canonical.service}}','body':'{{message}}'},
                 routing={'recipients':'team','mandatory_channels':[]},dedup_template='{{canonical.service}}:{{instrument}}',group_template='{{instrument}}',throttle_seconds=300,runbook_id=rb_pricing.id),
        ])
        chain=[t.id for t in teams]
        db.add_all([
            Source(name='HTTP Webhooks',slug='webhook',type='webhook',routing_chain=chain,configuration={}),
            Source(name='RabbitMQ Events',slug='broker',type='broker',routing_chain=chain,configuration={}),
            Source(name='Demo event generator',slug='demo',type='webhook',routing_chain=chain,configuration={}),
            Source(name='Pricing log lines',slug='logs',type='log',routing_chain=chain,configuration={'pattern':r'(?P<severity>WARN|ERROR|INFO)\s+(?P<service>pricing-engine-[\w-]+)\s+instrument=(?P<instrument>[\w-]+)\s+(?P<message>.+)' ,'mappings':{'severity':'severity','service':'service'}}),
        ])
    with session_scope() as db:
        source_web=db.query(Source).filter_by(slug='webhook').first()
        source_broker=db.query(Source).filter_by(slug='broker').first()
        ingest(db,source_broker,json.dumps(WORKFLOW),'seed-workflow')
        ingest(db,source_web,json.dumps(PAYMENTS),'seed-payments')
        ingest(db,source_broker,json.dumps(TRADE),'seed-trade')
        pump_events(db);process_deliveries(db)
        task=Ticket(team_id=payments.id,type='task',state='in_progress',title='Review circuit breaker thresholds',
                    body='Coordinate threshold adjustments with the reliability team.',severity='info',members=[avery.id],
                    created_at=now(),updated_at=now(),occurrences=0)
        db.add(task);db.flush();audit(db,'task_created',payments.id,task.id,avery)
        activity(db,payments.id,task.id,'task','Task created: '+task.title)
        workflow=db.query(Ticket).filter(Ticket.title.like('Settlement failure%')).first()
        if workflow:
            workflow.state='acknowledged';workflow.members=[avery.id];audit(db,'ticket_moved',workflow.team_id,workflow.id,avery,{'from':'new','to':'acknowledged'})
        payments_ticket=db.query(Ticket).filter(Ticket.title.like('High error rate%')).first()
        if payments_ticket:
            payments_ticket.state='in_progress';payments_ticket.members=[nora.id];audit(db,'ticket_moved',payments_ticket.team_id,payments_ticket.id,nora,{'from':'new','to':'in_progress'})
        rates_ticket=db.query(Ticket).filter(Ticket.title.like('Rejected EUR%')).first()
        if rates_ticket:
            rates_ticket.state='done';rates_ticket.done_by=sofia.id;rates_ticket.members=[sofia.id]
            audit(db,'ticket_moved',rates_ticket.team_id,rates_ticket.id,sofia,{'from':'new','to':'done'})

if __name__=='__main__':seed();print('Seeded SignalDesk demo database')
