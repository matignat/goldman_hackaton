"""Independent processor; horizontal workers use PostgreSQL SKIP LOCKED."""
import os,json,time,threading,logging
from .database import init_db,session_scope
from .models import Source
from .services.ingestion import ingest
from .services.routing import pump_events
from .services.delivery import process_deliveries
from .services.config_reload import reload_approved_rule_files
from .database import Session

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('signaldesk.worker')

def broker_consumer():
    """RabbitMQ source adapter: durable queue + broker manual ack AFTER database commit."""
    try:import pika
    except ImportError:
        log.warning('pika not installed; broker adapter unavailable');return
    url=os.getenv('RABBITMQ_URL')
    if not url:return
    while True:
        try:
            conn=pika.BlockingConnection(pika.URLParameters(url))
            channel=conn.channel();channel.queue_declare(queue='signaldesk.events',durable=True)
            channel.basic_qos(prefetch_count=50)
            def callback(ch,method,properties,body):
                try:
                    with session_scope() as db:
                        source=db.query(Source).filter_by(slug='broker').first()
                        if not source:raise RuntimeError('broker source is not configured')
                        ingest(db,source,body.decode('utf-8','replace'),getattr(properties,'message_id',None))
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception:
                    log.exception('Broker ingest failed, requeueing message')
                    ch.basic_nack(delivery_tag=method.delivery_tag,requeue=True)
                    time.sleep(1)
            channel.basic_consume(queue='signaldesk.events',on_message_callback=callback,auto_ack=False)
            channel.start_consuming()
        except Exception:
            log.exception('Broker disconnected; retrying')
            time.sleep(5)

def main():
    init_db()
    threading.Thread(target=broker_consumer,daemon=True).start()
    last_rules_scan=0
    while True:
        try:
            if time.monotonic()-last_rules_scan>2:
                reload_approved_rule_files(Session,os.getenv("APPROVED_RULE_CONFIG_DIR","config/rules"))
                last_rules_scan=time.monotonic()
            with session_scope() as db:
                count=pump_events(db,limit=500)
                deliveries=process_deliveries(db,limit=100)
            if count>=500:continue
            if count==0 and deliveries==0:time.sleep(float(os.getenv('WORKER_POLL_SECONDS','0.2')))
        except Exception:
            log.exception('Processing cycle failed; retrying safely')
            time.sleep(1)
if __name__=='__main__':main()
