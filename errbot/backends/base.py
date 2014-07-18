import inspect
import logging
import difflib
import traceback
from collections import deque, defaultdict
from xml.etree import cElementTree as ET
from xml.etree.cElementTree import ParseError

from errbot import botcmd, PY2
from errbot.utils import get_sender_username, xhtml2txt, parse_jid, split_string_after
from errbot.templating import tenv
from config import BOT_ADMINS, BOT_ASYNC, BOT_PREFIX, BOT_IDENTITY, CHATROOM_FN

try:
    from config import ACCESS_CONTROLS_DEFAULT
except ImportError:
    ACCESS_CONTROLS_DEFAULT = {}

try:
    from config import ACCESS_CONTROLS
except ImportError:
    ACCESS_CONTROLS = {}

try:
    from config import HIDE_RESTRICTED_COMMANDS
except ImportError:
    HIDE_RESTRICTED_COMMANDS = False

try:
    from config import HIDE_RESTRICTED_ACCESS
except ImportError:
    HIDE_RESTRICTED_ACCESS = False

try:
    from config import BOT_PREFIX_OPTIONAL_ON_CHAT
except ImportError:
    BOT_PREFIX_OPTIONAL_ON_CHAT = False

try:
    from config import BOT_ALT_PREFIXES
except ImportError:
    BOT_ALT_PREFIXES = ()

try:
    from config import BOT_ALT_PREFIX_SEPARATORS
except ImportError:
    BOT_ALT_PREFIX_SEPARATORS = ()

try:
    from config import BOT_ALT_PREFIX_CASEINSENSITIVE
except ImportError:
    BOT_ALT_PREFIX_CASEINSENSITIVE = False

try:
    from config import DIVERT_TO_PRIVATE
except ImportError:
    DIVERT_TO_PRIVATE = ()
    logging.warning("DIVERT_TO_PRIVATE is missing in config")
    pass

try:
    from config import MESSAGE_SIZE_LIMIT
except ImportError:
    MESSAGE_SIZE_LIMIT = 10000  # Corresponds with what HipChat accepts

if BOT_ASYNC:
    from errbot.bundled.threadpool import ThreadPool, WorkRequest


class ACLViolation(Exception):
    """Exceptions raised when user is not allowed to execute given command due to ACLs"""
    pass


class Identifier(object):
    """
    This class is the parent and the basic contract of all the ways the backends
    are identifying a person on their system.
    """

    def __init__(self, jid=None, node='', domain='', resource=''):
        if jid:
            self.node, self.domain, self.resource = parse_jid(jid)
        else:
            self.node = node
            self.domain = domain
            self.resource = resource

    def getNode(self):
        return self.node

    def getDomain(self):
        return self.domain

    def bareMatch(self, other):
        return other.getStripped() == self.getStripped()

    def getStripped(self):
        if self.domain:
            return self.node + '@' + self.domain
        return self.node  # if the backend has no domain notion

    def getResource(self):
        return self.resource

    def __str__(self):
        answer = self.getStripped()
        if self.resource:
            answer += '/' + self.resource
        return answer

    def __unicode__(self):
        return str(self.__str__())


class Presence(object):
    """
        An universal class representing protocol agnostic concept
        of presence.
    """

    def __init__(self, nick, room, real_id=None):
        self.nick = nick
        self.room = room
        self.real_id = real_id

    def get_room(self):
        return self.room

    def get_nick(self):
        return self.nick

    def get_real_name(self):
        return self.real_id


class Message(object):
    fr = Identifier('unknown@localhost')

    def __init__(self, body, typ='chat', html=None):
        # it is either unicode or assume it is utf-8
        if isinstance(body, str):
            self.body = body
        else:
            self.body = body.decode('utf-8')
        self.html = html
        self.typ = typ
        self.delayed = False
        self.mucknick = None

    def setTo(self, to):
        if isinstance(to, Identifier):
            self.to = to
        else:
            self.to = Identifier(to)  # assume a parseable string

    def getTo(self):
        return self.to

    def setType(self, typ):
        self.typ = typ

    def getType(self):
        return self.typ

    def getFrom(self):
        return self.fr

    def setFrom(self, fr):
        if isinstance(fr, Identifier):
            self.fr = fr
        else:
            self.fr = Identifier(fr)  # assume a parseable string

    def getBody(self):
        return self.body

    def getHTML(self):
        return self.html

    def setHTML(self, html):
        self.html = html

    def setDelayed(self, delayed):
        self.delayed = delayed

    def isDelayed(self):
        return self.delayed

    def setMuckNick(self, nick):
        self.mucknick = nick

    def getMuckNick(self):
        return self.mucknick

    def __str__(self):
        return self.body


class Connection(object):
    def send_message(self, mess):
        raise NotImplementedError("It should be implemented specifically for your backend")


def build_text_html_message_pair(source):
    node = None
    text_plain = None

    try:
        node = ET.XML(source)
        text_plain = xhtml2txt(source)
    except ParseError as ee:
        if source.strip():  # avoids keep alive pollution
            logging.debug('Could not parse [%s] as XHTML-IM, assume pure text Parsing error = [%s]' % (source, ee))
            text_plain = source
    return text_plain, node


def build_message(text, message_class, conversion_function=None):
    """Builds an xhtml message without attributes.
    If input is not valid xhtml-im fallback to normal."""
    message = None  # keeps the compiler happy
    try:
        text = text.replace('', '*')  # there is a weird chr IRC is sending that we need to filter out
        if PY2:
            ET.XML(text.encode('utf-8'))  # test if is it xml
        else:
            ET.XML(text)

        edulcorated_html = conversion_function(text) if conversion_function else text
        try:
            text_plain, node = build_text_html_message_pair(edulcorated_html)
            message = message_class(body=text_plain)
            message.setHTML(node)
        except ET.ParseError as ee:
            logging.error('Error translating to hipchat [%s] Parsing error = [%s]' % (edulcorated_html, ee))
    except ET.ParseError as ee:
        if text.strip():  # avoids keep alive pollution
            logging.debug('Determined that [%s] is not XHTML-IM (%s)' % (text, ee))
        message = message_class(body=text)
    return message


class Backend(object):
    """
    Implements the basic Bot logic (logic independent from the backend) and leaves
    you to implement the missing parts
    """

    cmd_history = defaultdict(lambda: deque(maxlen=10))  # this will be a per user history

    MSG_ERROR_OCCURRED = 'Sorry for your inconvenience. ' \
                         'An unexpected error occurred.'
    MESSAGE_SIZE_LIMIT = MESSAGE_SIZE_LIMIT
    MSG_UNKNOWN_COMMAND = 'Unknown command: "%(command)s". ' \
                          'Type "' + BOT_PREFIX + 'help" for available commands.'
    MSG_HELP_TAIL = 'Type help <command name> to get more info ' \
                    'about that specific command.'
    MSG_HELP_UNDEFINED_COMMAND = 'That command is not defined.'

    def __init__(self, *args, **kwargs):
        """ Those arguments will be directly those put in BOT_IDENTITY
        """
        if BOT_ASYNC:
            self.thread_pool = ThreadPool(3)
            logging.debug('created the thread pool' + str(self.thread_pool))
        self.commands = {}  # the dynamically populated list of commands available on the bot
        self.re_commands = {}  # the dynamically populated list of regex-based commands available on the bot

        if BOT_ALT_PREFIX_CASEINSENSITIVE:
            self.bot_alt_prefixes = tuple(prefix.lower() for prefix in BOT_ALT_PREFIXES)
        else:
            self.bot_alt_prefixes = BOT_ALT_PREFIXES

    def send_message(self, mess):
        """Send a message"""
        self.connect().send_message(mess)

    def send_simple_reply(self, mess, text, private=False):
        """Send a simple response to a message"""
        self.send_message(self.build_reply(mess, text, private))

    def build_reply(self, mess, text=None, private=False):
        """Build a message for responding to another message.
        Message is NOT sent"""
        response = self.build_message(text)
        msg_type = mess.getType()

        response.setFrom(self.jid)
        if msg_type == 'groupchat' and not private:
            # getStripped() returns the full bot@conference.domain.tld/chat_username
            # but in case of a groupchat, we should only try to send to the MUC address
            # itself (bot@conference.domain.tld)
            response.setTo(mess.getFrom().getStripped().split('/')[0])
        elif str(mess.getTo()) == BOT_IDENTITY['username']:
            # This is a direct private message, not initiated through a MUC. Use
            # getStripped() to remove the resource so that the response goes to the
            # client with the highest priority
            response.setTo(mess.getFrom().getStripped())
        else:
            # This is a private message that was initiated through a MUC. Don't use
            # getStripped() here to retain the resource, else the XMPP server doesn't
            # know which user we're actually responding to.
            response.setTo(mess.getFrom())
        response.setType('chat' if private else msg_type)
        return response

    def callback_message(self, conn, mess):
        """
        Needs to return False if we want to stop further treatment
        """
        # Prepare to handle either private chats or group chats
        type = mess.getType()
        jid = mess.getFrom()
        text = mess.getBody()
        username = get_sender_username(mess)
        user_cmd_history = self.cmd_history[username]

        if mess.isDelayed():
            logging.debug("Message from history, ignore it")
            return False

        if type not in ("groupchat", "chat"):
            logging.debug("unhandled message type %s" % mess)
            return False

        # Ignore messages from ourselves. Because it isn't always possible to get the
        # real JID from a MUC participant (including ourself), matching the JID against
        # ourselves isn't enough (see https://github.com/gbin/err/issues/90 for
        # background discussion on this). Matching against CHATROOM_FN isn't technically
        # correct in all cases because a MUC could give us another nickname, but it
        # covers 99% of the MUC cases, so it should suffice for the time being.
        if (jid.bareMatch(Identifier(self.jid)) or
            type == "groupchat" and mess.getMuckNick() == CHATROOM_FN):  # noqa
                logging.debug("Ignoring message from self")
                return False

        logging.debug("*** jid = %s" % jid)
        logging.debug("*** username = %s" % username)
        logging.debug("*** type = %s" % type)
        logging.debug("*** text = %s" % text)

        # If a message format is not supported (eg. encrypted),
        # txt will be None
        if not text:
            return False

        surpress_cmd_not_found = False

        prefixed = False  # Keeps track whether text was prefixed with a bot prefix
        only_check_re_command = False  # Becomes true if text is determed to not be a regular command
        tomatch = text.lower() if BOT_ALT_PREFIX_CASEINSENSITIVE else text
        if len(BOT_ALT_PREFIXES) > 0 and tomatch.startswith(self.bot_alt_prefixes):
            # Yay! We were called by one of our alternate prefixes. Now we just have to find out
            # which one... (And find the longest matching, in case you have 'err' and 'errbot' and
            # someone uses 'errbot', which also matches 'err' but would leave 'bot' to be taken as
            # part of the called command in that case)
            prefixed = True
            longest = 0
            for prefix in self.bot_alt_prefixes:
                l = len(prefix)
                if tomatch.startswith(prefix) and l > longest:
                    longest = l
            logging.debug("Called with alternate prefix '{}'".format(text[:longest]))
            text = text[longest:]

            # Now also remove the separator from the text
            for sep in BOT_ALT_PREFIX_SEPARATORS:
                # While unlikely, one may have separators consisting of
                # more than one character
                l = len(sep)
                if text[:l] == sep:
                    text = text[l:]
        elif type == "chat" and BOT_PREFIX_OPTIONAL_ON_CHAT:
            logging.debug("Assuming '%s' to be a command because BOT_PREFIX_OPTIONAL_ON_CHAT is True" % text)
            # In order to keep noise down we surpress messages about the command
            # not being found, because it's possible a plugin will trigger on what
            # was said with trigger_message.
            surpress_cmd_not_found = True
        elif not text.startswith(BOT_PREFIX):
            only_check_re_command = True
        if text.startswith(BOT_PREFIX):
            text = text[len(BOT_PREFIX):]
            prefixed = True

        text = text.strip()
        text_split = text.split(' ')
        cmd = None
        command = None
        args = ''
        if not only_check_re_command:
            if len(text_split) > 1:
                command = (text_split[0] + '_' + text_split[1]).lower()
                if command in self.commands:
                    cmd = command
                    args = ' '.join(text_split[2:])

            if not cmd:
                command = text_split[0].lower()
                args = ' '.join(text_split[1:])
                if command in self.commands:
                    cmd = command
                    if len(text_split) > 1:
                        args = ' '.join(text_split[1:])

            if command == BOT_PREFIX:  # we did "!!" so recall the last command
                if len(user_cmd_history):
                    cmd, args = user_cmd_history[-1]
                else:
                    return False  # no command in history
            elif command.isdigit():  # we did "!#" so we recall the specified command
                index = int(command)
                if len(user_cmd_history) >= index:
                    cmd, args = user_cmd_history[-index]
                else:
                    return False  # no command in history

        # Try to match one of the regex commands if the regular commands produced no match
        matched_on_re_command = False
        if not cmd:
            if prefixed:
                commands = self.re_commands
            else:
                commands = {k: self.re_commands[k] for k in self.re_commands
                            if not self.re_commands[k]._err_command_prefix_required}

            for name, func in commands.items():
                match = func._err_command_re_pattern.search(text)
                if match:
                    logging.debug("Matching '{}' against '{}' produced a match"
                                  .format(text, func._err_command_re_pattern.pattern))
                    matched_on_re_command = True
                    self._process_command(mess, name, text, match)
                else:
                    logging.debug("Matching '{}' against '{}' produced no match"
                                  .format(text, func._err_command_re_pattern.pattern))
        if matched_on_re_command:
            return True

        if cmd:
            self._process_command(mess, cmd, args, match=None)
        elif not only_check_re_command:
            logging.debug("Command not found")
            if surpress_cmd_not_found:
                logging.debug("Surpressing command not found feedback")
            else:
                reply = self.unknown_command(mess, command, args)
                if reply is None:
                    reply = self.MSG_UNKNOWN_COMMAND % {'command': command}
                if reply:
                    self.send_simple_reply(mess, reply)
        return True

    def _process_command(self, mess, cmd, args, match):
        """Process and execute a bot command"""
        logging.info("Processing command {} with parameters '{}'".format(cmd, args))

        jid = mess.getFrom()
        username = get_sender_username(mess)
        user_cmd_history = self.cmd_history[username]

        if (cmd, args) in user_cmd_history:
            user_cmd_history.remove((cmd, args))  # Avoids duplicate history items

        try:
            self.check_command_access(mess, cmd)
        except ACLViolation as e:
            if not HIDE_RESTRICTED_ACCESS:
                self.send_simple_reply(mess, str(e))
            return

        f = self.re_commands[cmd] if match else self.commands[cmd]

        if f._err_command_admin_only and BOT_ASYNC:
            # If it is an admin command, wait until the queue is completely depleted so
            # we don't have strange concurrency issues on load/unload/updates etc...
            self.thread_pool.wait()

        if f._err_command_historize:
            user_cmd_history.append((cmd, args))  # add it to the history only if it is authorized to be so

        # Don't check for None here as None can be a valid argument to split.
        # '' was chosen as default argument because this isn't a valid argument to split()
        if not match and f._err_command_split_args_with != '':
            args = args.split(f._err_command_split_args_with)
        if BOT_ASYNC:
            wr = WorkRequest(
                self._execute_and_send,
                [],
                {'cmd': cmd, 'args': args, 'match': match, 'mess': mess, 'jid': jid,
                 'template_name': f._err_command_template}
            )
            self.thread_pool.putRequest(wr)
            if f._err_command_admin_only:
                # Again, if it is an admin command, wait until the queue is completely
                # depleted so we don't have strange concurrency issues.
                self.thread_pool.wait()
        else:
            self._execute_and_send(cmd=cmd, args=args, match=match, mess=mess, jid=jid,
                                   template_name=f._err_command_template)

    def _execute_and_send(self, cmd, args, match, mess, jid, template_name=None):
        """Execute a bot command and send output back to the caller

        cmd: The command that was given to the bot (after being expanded)
        args: Arguments given along with cmd
        match: A re.MatchObject if command is coming from a regex-based command, else None
        mess: The message object
        jid: The jid of the person executing the command
        template_name: The name of the template which should be used to render
            html-im output, if any

        """

        def process_reply(reply):
            # integrated templating
            if template_name:
                reply = tenv().get_template(template_name + '.html').render(**reply)

            # Reply should be all text at this point (See https://github.com/gbin/err/issues/96)
            return str(reply)

        def send_reply(reply):
            for part in split_string_after(reply, self.MESSAGE_SIZE_LIMIT):
                self.send_simple_reply(mess, part, cmd in DIVERT_TO_PRIVATE)

        commands = self.re_commands if match else self.commands
        try:
            if inspect.isgeneratorfunction(commands[cmd]):
                replies = commands[cmd](mess, match) if match else commands[cmd](mess, args)
                for reply in replies:
                    if reply:
                        send_reply(process_reply(reply))
            else:
                reply = commands[cmd](mess, match) if match else commands[cmd](mess, args)
                if reply:
                    send_reply(process_reply(reply))
        except Exception as e:
            tb = traceback.format_exc()
            logging.exception('An error happened while processing '
                              'a message ("%s") from %s: %s"' %
                              (mess.getBody(), jid, tb))
            send_reply(self.MSG_ERROR_OCCURRED + ':\n %s' % e)

    def check_command_access(self, mess, cmd):
        """
        Check command against ACL rules

        Raises ACLViolation() if the command may not be executed in the given context
        """
        usr = str(get_jid_from_message(mess))
        typ = mess.getType()

        if cmd not in ACCESS_CONTROLS:
            ACCESS_CONTROLS[cmd] = ACCESS_CONTROLS_DEFAULT

        if 'allowusers' in ACCESS_CONTROLS[cmd] and usr not in ACCESS_CONTROLS[cmd]['allowusers']:
            raise ACLViolation("You're not allowed to access this command from this user")
        if 'denyusers' in ACCESS_CONTROLS[cmd] and usr in ACCESS_CONTROLS[cmd]['denyusers']:
            raise ACLViolation("You're not allowed to access this command from this user")
        if typ == 'groupchat':
            stripped = mess.getFrom().getStripped()
            if 'allowmuc' in ACCESS_CONTROLS[cmd] and ACCESS_CONTROLS[cmd]['allowmuc'] is False:
                raise ACLViolation("You're not allowed to access this command from a chatroom")
            if 'allowrooms' in ACCESS_CONTROLS[cmd] and stripped not in ACCESS_CONTROLS[cmd]['allowrooms']:
                raise ACLViolation("You're not allowed to access this command from this room")
            if 'denyrooms' in ACCESS_CONTROLS[cmd] and stripped in ACCESS_CONTROLS[cmd]['denyrooms']:
                raise ACLViolation("You're not allowed to access this command from this room")
        else:
            if 'allowprivate' in ACCESS_CONTROLS[cmd] and ACCESS_CONTROLS[cmd]['allowprivate'] is False:
                raise ACLViolation("You're not allowed to access this command via private message to me")

        f = self.commands[cmd] if cmd in self.commands else self.re_commands[cmd]

        if f._err_command_admin_only:
            if typ == 'groupchat':
                raise ACLViolation("You cannot administer the bot from a chatroom, message the bot directly")
            if usr not in BOT_ADMINS:
                raise ACLViolation("This command requires bot-admin privileges")

    def unknown_command(self, mess, cmd, args):
        """ Override the default unknown command behavior
        """
        full_cmd = cmd + ' ' + args.split(' ')[0] if args else None
        if full_cmd:
            part1 = 'Command "%s" / "%s" not found.' % (cmd, full_cmd)
        else:
            part1 = 'Command "%s" not found.' % cmd
        ununderscore_keys = [m.replace('_', ' ') for m in self.commands.keys()]
        matches = difflib.get_close_matches(cmd, ununderscore_keys)
        if full_cmd:
            matches.extend(difflib.get_close_matches(full_cmd, ununderscore_keys))
        matches = set(matches)
        if matches:
            return part1 + '\n\nDid you mean "' + BOT_PREFIX + ('" or "' + BOT_PREFIX).join(matches) + '" ?'
        else:
            return part1

    def inject_commands_from(self, instance_to_inject):
        classname = instance_to_inject.__class__.__name__
        for name, value in inspect.getmembers(instance_to_inject, inspect.ismethod):
            if getattr(value, '_err_command', False):
                commands = self.re_commands if getattr(value, '_err_re_command') else self.commands
                name = getattr(value, '_err_command_name')

                if name in commands:
                    f = commands[name]
                    new_name = (classname + '-' + name).lower()
                    self.warn_admins('%s.%s clashes with %s.%s so it has been renamed %s' % (
                        classname, name, type(f.__self__).__name__, f.__name__, new_name))
                    name = new_name
                commands[name] = value

                if getattr(value, '_err_re_command'):
                    logging.debug('Adding regex command : %s -> %s' % (name, value.__name__))
                    self.re_commands = commands
                else:
                    logging.debug('Adding command : %s -> %s' % (name, value.__name__))
                    self.commands = commands

    def remove_commands_from(self, instance_to_inject):
        for name, value in inspect.getmembers(instance_to_inject, inspect.ismethod):
            if getattr(value, '_err_command', False):
                name = getattr(value, '_err_command_name')
                if getattr(value, '_err_re_command') and name in self.re_commands:
                    del (self.re_commands[name])
                elif not getattr(value, '_err_re_command') and name in self.commands:
                    del (self.commands[name])

    def warn_admins(self, warning):
        for admin in BOT_ADMINS:
            self.send(admin, warning)

    def top_of_help_message(self):
        """Returns a string that forms the top of the help message

        Override this method in derived class if you
        want to add additional help text at the
        beginning of the help message.
        """
        return ""

    def bottom_of_help_message(self):
        """Returns a string that forms the bottom of the help message

        Override this method in derived class if you
        want to add additional help text at the end
        of the help message.
        """
        return ""

    @botcmd
    def help(self, mess, args):
        """   Returns a help string listing available options.

        Automatically assigned to the "help" command."""
        if not args:
            if self.__doc__:
                description = self.__doc__.strip()
            else:
                description = 'Available commands:'

            usage = '\n'.join(sorted([
                BOT_PREFIX + '%s: %s' % (name, (command.__doc__ or
                                                '(undocumented)').strip().split('\n', 1)[0])
                for (name, command) in self.commands.iteritems()
                if name != 'help'
                and not command._err_command_hidden
            ]))
            usage = '\n\n' + '\n\n'.join(filter(None, [usage, self.MSG_HELP_TAIL]))
        else:
            description = ''
            if args in self.commands:
                usage = (self.commands[args].__doc__ or
                         'undocumented').strip()
            else:
                usage = self.MSG_HELP_UNDEFINED_COMMAND

        top = self.top_of_help_message()
        bottom = self.bottom_of_help_message()
        return ''.join(filter(None, [top, description, usage, bottom]))

    def send(self, user, text, in_reply_to=None, message_type='chat'):
        """Sends a simple message to the specified user."""
        mess = self.build_message(text)
        if hasattr(user, 'getStripped'):
            mess.setTo(user.getStripped())
        else:
            mess.setTo(user)

        if in_reply_to:
            mess.setType(in_reply_to.getType())
            mess.setFrom(in_reply_to.getTo().getStripped())
        else:
            mess.setType(message_type)
            mess.setFrom(self.jid)

        self.send_message(mess)

    # ##### HERE ARE THE SPECIFICS TO IMPLEMENT PER BACKEND

    def build_message(self, text):
        raise NotImplementedError("It should be implemented specifically for your backend")

    def serve_forever(self):
        raise NotImplementedError("It should be implemented specifically for your backend")

    def connect(self):
        """Connects the bot to server or returns current connection
        """
        raise NotImplementedError("It should be implemented specifically for your backend")

    def join_room(self, room, username=None, password=None):
        raise NotImplementedError("It should be implemented specifically for your backend")

    def shutdown(self):
        pass

    def connect_callback(self):
        pass

    def disconnect_callback(self):
        pass

    def callback_contact_online(self, conn, pres):
        pass

    def callback_contact_offline(self, conn, pres):
        pass

    def callback_user_joined_chat(self, conn, pres):
        pass

    def callback_user_left_chat(self, conn, pres):
        pass

    @property
    def mode(self):
        raise NotImplementedError("It should be implemented specifically for your backend")


def get_jid_from_message(mess):
    if mess.getType() == 'chat':
        # strip the resource for direct chats
        return str(mess.getFrom().getStripped())
    fr = mess.getFrom()
    jid = Identifier(node=fr.node, domain=fr.domain, resource=fr.resource)
    return jid
