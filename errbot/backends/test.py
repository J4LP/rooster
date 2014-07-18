from queue import Queue
import logging
from os.path import sep, abspath
import sys
from tempfile import mkdtemp
from threading import Thread
import unittest
from errbot.main import main

__import__('errbot.config-template')
config_module = sys.modules['errbot.config-template']
sys.modules['config'] = config_module

tempdir = mkdtemp()
config_module.BOT_DATA_DIR = tempdir
config_module.BOT_LOG_FILE = tempdir + sep + 'log.txt'
config_module.BOT_EXTRA_PLUGIN_DIR = []
config_module.BOT_LOG_LEVEL = logging.DEBUG

from errbot.backends.base import Message, build_message
from errbot.errBot import ErrBot
from errbot.builtins.wsview import reset_app

incoming_stanza_queue = Queue()
outgoing_message_queue = Queue()

QUIT_MESSAGE = '$STOP$'

STZ_MSG = 1
STZ_PRE = 2
STZ_IQ = 3


class ConnectionMock():
    def send(self, mess):
        outgoing_message_queue.put(mess.getBody())

    def send_message(self, mess):
        self.send(mess)


class TestBackend(ErrBot):
    conn = ConnectionMock()

    def serve_forever(self):
        import config

        self.jid = 'Err@localhost'  # whatever
        self.connect()  # be sure we are "connected" before the first command
        self.connect_callback()  # notify that the connection occured
        self.sender = config.BOT_ADMINS[0]  # By default, assume this is the admin talking
        try:
            while True:
                stanza_type, entry = incoming_stanza_queue.get()
                if entry == QUIT_MESSAGE:
                    logging.info("Stop magic message received, quitting...")
                    break
                if stanza_type is STZ_MSG:
                    msg = Message(entry)
                    msg.setFrom(self.sender)
                    msg.setTo(self.jid)  # To me only
                    self.callback_message(self.conn, msg)
                elif stanza_type is STZ_PRE:
                    logging.info("Presence stanza received.")
                elif stanza_type is STZ_IQ:
                    logging.info("IQ stanza received.")
                else:
                    logging.error("Unknown stanza type.")

        except EOFError as _:
            pass
        except KeyboardInterrupt as _:
            pass
        finally:
            logging.debug("Trigger disconnect callback")
            self.disconnect_callback()
            logging.debug("Trigger shutdown")
            self.shutdown()

    def connect(self):
        if not self.conn:
            self.conn = ConnectionMock()
        return self.conn

    def build_message(self, text):
        return build_message(text, Message)

    def shutdown(self):
        super(TestBackend, self).shutdown()

    def join_room(self, room, username=None, password=None):
        pass  # just ignore that

    @property
    def mode(self):
        return 'text'


def popMessage(timeout=5, block=True):
    return outgoing_message_queue.get(timeout=timeout, block=block)


def pushMessage(msg):
    incoming_stanza_queue.put((STZ_MSG, msg), timeout=5)


def pushPresence(stanza):
    pass


# def pushIQ(stanza):
#    pass

def zapQueues():
    while not incoming_stanza_queue.empty():
        msg = incoming_stanza_queue.get(block=False)
        logging.error('Message left in the incoming queue during a test : %s' % msg)

    while not outgoing_message_queue.empty():
        msg = outgoing_message_queue.get(block=False)
        logging.error('Message left in the outgoing queue during a test : %s' % msg)


class FullStackTest(unittest.TestCase):
    """ This class starts a full bot with a test backend so you can add unit tests on an almost complete bot
    """
    bot_thread = None

    def setUp(self, extra_test_file=None, loglevel=logging.DEBUG):
        # reset logging to console
        logging.basicConfig(format='%(levelname)s:%(message)s')
        console = logging.StreamHandler()
        file = logging.FileHandler(config_module.BOT_LOG_FILE, encoding='utf-8')
        logger = logging.getLogger('')
        logger.setLevel(loglevel)
        logger.addHandler(file)
        if extra_test_file:
            import config

            config.BOT_EXTRA_PLUGIN_DIR = sep.join(abspath(extra_test_file).split(sep)[:-2])
        self.bot_thread = Thread(target=main, name='Test Bot Thread', args=(TestBackend, logger))
        self.bot_thread.setDaemon(True)
        self.bot_thread.start()

    def tearDown(self):
        pushMessage(QUIT_MESSAGE)
        self.bot_thread.join()
        reset_app()  # empty the bottle ... hips!
        logging.info("Main bot thread quits")
        zapQueues()

    def assertCommand(self, command, response, timeout=5):
        pushMessage(command)
        self.assertIn(response, popMessage(), timeout)

    def assertCommandFound(self, command, timeout=5):
        pushMessage(command)
        self.assertNotIn('not found', popMessage(), timeout)
