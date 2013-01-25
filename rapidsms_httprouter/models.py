import datetime
from django.db import models, transaction
#import django
import django.dispatch
from django.db import connection as db_connection
from rapidsms.models import Contact, Connection

from .managers import ForUpdateManager
from django.conf import settings

mass_text_sent = django.dispatch.Signal(providing_args=["messages", "status"])

DIRECTION_CHOICES = (
    ('I', "Incoming"),
    ('O', "Outgoing"))

STATUS_CHOICES = (
    ('R', "Received"),
    ('H', "Handled"),

    ('P', "Processing"),
    ('L', "Locked"),

    ('Q', "Queued"),
    ('S', "Sent"),
    ('D', "Delivered"),

    ('C', "Cancelled"),
    ('E', "Errored")
)

# <<<<<<< HEAD
# class Message(models.Model):
#     connection = models.ForeignKey(Connection, related_name='messages')
#     text       = models.TextField()
# 
#     direction  = models.CharField(max_length=1, choices=DIRECTION_CHOICES)
#     status     = models.CharField(max_length=1, choices=STATUS_CHOICES)
# 
#     date       = models.DateTimeField(auto_now_add=True)
#     updated    = models.DateTimeField(auto_now=True, null=True)
# 
#     sent       = models.DateTimeField(null=True, blank=True)
#     delivered  = models.DateTimeField(null=True, blank=True)
# 
#     in_response_to = models.ForeignKey('self', related_name='responses', null=True, blank=True)
# =======
#
# Allows us to use SQL to lock a row when setting it to 'locked'.  Without this
# in a multi-process environment like Gunicorn we'll double send messages.
#
# See: https://coderanger.net/2011/01/select-for-update/
#
class MessageBatch(models.Model):
    status = models.CharField(max_length=1, choices=STATUS_CHOICES)
    name = models.CharField(max_length=15,null=True,blank=True)

class Message(models.Model):
    connection = models.ForeignKey(Connection, related_name='messages')
    text = models.TextField(db_index=True)
    direction = models.CharField(max_length=1, choices=DIRECTION_CHOICES, db_index=True)
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, db_index=True)
    date = models.DateTimeField(auto_now_add=True)
    priority = models.IntegerField(default=10, db_index=True)

    in_response_to = models.ForeignKey('self', related_name='responses', null=True)
    application = models.CharField(max_length=100, null=True)

    batch = models.ForeignKey(MessageBatch, related_name='messages', null=True)
    # set our manager to our update manager
    objects = ForUpdateManager()

    def __unicode__(self):
        # crop the text (to avoid exploding the admin)
        if len(self.text) < 60: str = self.text
        else: str = "%s..." % (self.text[0:57])

        to_from = (self.direction == "I") and "to" or "from"
        return "%s (%s %s)" % (str, to_from, self.connection.identity)

    def as_json(self):
        return dict(id=self.pk,
                    contact=self.connection.identity, backend=self.connection.backend.name,
                    direction=self.direction, status=self.status, text=self.text,
                    date=self.date.isoformat())

    def send(self):
        """
        Triggers our celery task to send this message off.  Note that our dependency to Celery
        is a soft one, as we only do the import of Tasks here.  If a user has ROUTER_URL
        set to NONE (say when using an Android relayer) then there is no need for Celery and friends.
        """
        from tasks import send_message_task

        # send this message off in celery
        send_message_task.delay(self.pk)

class DeliveryError(models.Model):
    """
    Simple class to keep track of delivery errors for messages.  We retry up to three times before
    finally giving up on sending.
    """
    message = models.ForeignKey(Message, related_name='errors',
                                help_text="The message that had an error")
    log = models.TextField(help_text="A short log on the error that was received when this message was delivered")
    created_on = models.DateTimeField(auto_now_add=True,
                                      help_text="When this delivery error occurred")
                                      
    @classmethod
    @transaction.commit_on_success
    def mass_text(cls, text, connections, status='P', batch_status='Q'):
        batch = MessageBatch.objects.create(status=batch_status)
        sql = 'insert into rapidsms_httprouter_message (text, date, direction, status, batch_id, connection_id, priority) values '
        insert_list = []
        params_list = []
        d = datetime.datetime.now()
        c = db_connection.cursor()

        for connection in connections:
            insert_list.append("(%s, %s, 'O', %s, %s, %s, %s)")
            params_list += [text, d, status, batch.pk, connection.pk, 10]

        sql = "%s %s returning id" % (sql, ",".join(insert_list))
        c.execute(sql, params_list)

        pks = c.fetchall()
        toret = Message.objects.filter(pk__in=[pk[0] for pk in pks])
        mass_text_sent.send(sender=batch, messages=toret, status=status)
        return toret


