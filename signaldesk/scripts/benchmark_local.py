#!/usr/bin/env python3
"""Real durable pipeline microbenchmark using a fresh local SQLite DB.

Measures event ingestion, processing, and durable captured channel delivery.
Not a measurement of RabbitMQ/Postgres network, SSE browser receipt, or SMTP receipt.
Use the Docker environment for full-system timing in a real demonstration.
"""
import argparse,copy,json,os,statistics,sys,tempfile,time,uuid
from datetime import datetime,timezone
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('--burst',type=int,default=500)
parser.add_argument('--mixed',type=int,default=80)
parser.add_argument('--unmatched',type=int,default=20)
parser.add_argument('--output',default='benchmark-results.json')
args=parser.parse_args()
if min(args.burst,args.mixed,args.unmatched)<0:parser.error('counts must be non-negative')
filename=Path(tempfile.gettempdir())/f'signaldesk_benchmark_{uuid.uuid4().hex}.sqlite'
os.environ['DATABASE_URL']='sqlite:///'+str(filename)
os.environ['DEMO_INLINE_WORKER']='0'
os.environ.pop('SMTP_HOST',None)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.seed import seed,PAYMENTS,WORKFLOW
from app.database import Session,engine
from app.models import Source,Event,Ticket,Delivery
from app.services.ingestion import ingest
from app.services.routing import pump_events
from app.services.delivery import process_deliveries
from sqlalchemy import func

def aware(v):return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
def percent(values,p):
    if not values:return None
    values=sorted(values)
    return round(values[min(len(values)-1,max(0,int((len(values)-1)*p)))],2)

def out_metric(count,span):return round(count/span,2) if span else None
try:
    seed()
    with Session() as db:
        src=db.query(Source).filter_by(slug='demo').one()
        baseline=db.query(Event).count();baseline_tickets=db.query(Ticket).count()
        payment=copy.deepcopy(PAYMENTS);payment['labels']['region']='benchmark-unique-region';payment['startsAt']=datetime.now(timezone.utc).isoformat()
        events=[payment]*args.burst
        for i in range(args.mixed):
            w=copy.deepcopy(WORKFLOW);w['workflowId']=f'BENCH-{uuid.uuid4().hex[:8]}-{i}'
            w['timestamp']=datetime.now(timezone.utc).isoformat();events.append(w)
        for i in range(args.unmatched):events.append({'neverSeen':{'new':i,'nested':{'reason':'benchmark'}},'timestamp':datetime.now(timezone.utc).isoformat()})
        start=time.perf_counter()
        for i,payload in enumerate(events):ingest(db,src,json.dumps(payload),f'bench-{i}')
        db.commit();ingest_sec=time.perf_counter()-start
        start=time.perf_counter()
        while pump_events(db,limit=400):db.commit()
        process_sec=time.perf_counter()-start
        start=time.perf_counter()
        for i in range(30):
            n=process_deliveries(db,limit=500);db.commit()
            if not n:break
        channel_sec=time.perf_counter()-start
        emitted=db.query(Event).filter(Event.id>baseline).all()
        latencies=[(aware(e.processed_at)-aware(e.received_at)).total_seconds()*1000 for e in emitted if e.processed_at]
        new_tickets=db.query(Ticket).filter(Ticket.id>baseline_tickets).all()
        burst_ticket=next((t for t in new_tickets if t.schema=='HighErrorRate'),None)
        new_ids=[t.id for t in new_tickets]
        deliveries=db.query(Delivery).filter(Delivery.ticket_id.in_(new_ids)).all() if new_ids else []
        capture_latencies=[]
        for d in deliveries:
            if d.status!='sent':continue
            e=db.get(Event,db.get(Ticket,d.ticket_id).event_id)
            if e:capture_latencies.append((aware(d.sent_at)-aware(e.received_at)).total_seconds()*1000)
        result={
            'system':'SQLite local single-process test; real DB writes, deterministic matching, captured channel adapters',
            'measured_at':datetime.now(timezone.utc).isoformat(),'input':{'burst':args.burst,'mixed':args.mixed,'unmatched':args.unmatched},
            'ingestion':{'events':len(events),'seconds':round(ingest_sec,3),'events_per_second':out_metric(len(events),ingest_sec)},
            'matching':{'seconds':round(process_sec,3),'events_per_second':out_metric(len(events),process_sec),
                        'ingest_to_processing_p50_ms':percent(latencies,.5),'ingest_to_processing_p99_ms':percent(latencies,.99),
                        'processed':sum(e.state in ('matched','unmatched','rejected') for e in emitted),
                        'unmatched':sum(e.state=='unmatched' for e in emitted)},
            'delivery':{'processing_seconds':round(channel_sec,3),'jobs_created':len(deliveries),
                        'sent':sum(d.status=='sent' for d in deliveries),
                        'queued_or_held':sum(d.status in ('held','queued') for d in deliveries),
                        'ingest_to_captured_channel_p50_ms':percent(capture_latencies,.5),
                        'ingest_to_captured_channel_p99_ms':percent(capture_latencies,.99)},
            'burst_result':{'single_ticket':bool(burst_ticket and sum(t.schema=='HighErrorRate' for t in new_tickets)==1),
                            'occurrences':burst_ticket.occurrences if burst_ticket else None,
                            'new_ticket_delivery_jobs':sum(d.ticket_id==burst_ticket.id and d.kind=='new_ticket' for d in deliveries) if burst_ticket else 0,
                            'note':'one delivery job per selected recipient/channel; not one global delivery'},
            'caveats':'SQLite and capture-only channels; UI SSE browser push and real SMTP/network delivery were NOT measured.'}
        path=Path(args.output);path.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
finally:
    engine.dispose()
    for ext in ('','-shm','-wal'):
        f=Path(str(filename)+ext)
        if f.exists():f.unlink()
