"""Publish Appendix A examples to the real RabbitMQ source (not an HTTP alias)."""
import os,json,uuid,sys
import pika
from app.seed import WORKFLOW,TRADE

def main():
    url=os.getenv('RABBITMQ_URL','amqp://guest:guest@localhost:5672/%2F')
    connection=pika.BlockingConnection(pika.URLParameters(url))
    ch=connection.channel();ch.queue_declare(queue='signaldesk.events',durable=True)
    payload=json.loads(sys.stdin.read()) if not sys.stdin.isatty() else WORKFLOW
    ch.basic_publish(exchange='',routing_key='signaldesk.events',body=json.dumps(payload).encode(),
                     properties=pika.BasicProperties(delivery_mode=2,message_id=str(uuid.uuid4())),mandatory=True)
    connection.close()
    print('Published one persistent RabbitMQ message')
if __name__=='__main__':main()
