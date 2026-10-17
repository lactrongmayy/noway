# Commands package
"""Danh sách 20 nhóm lệnh prefix của bot."""

from .chat import COMMANDS as chat
from .member import COMMANDS as member
from .server import COMMANDS as server
from .moderation import COMMANDS as moderation
from .channel import COMMANDS as channel
from .role import COMMANDS as role
from .logs import COMMANDS as logs
from .security import COMMANDS as security
from .welcome import COMMANDS as welcome
from .level import COMMANDS as level
from .games import COMMANDS as games
from .social import COMMANDS as social
from .math import COMMANDS as math
from .conversion import COMMANDS as conversion
from .time import COMMANDS as time
from .encoding import COMMANDS as encoding
from .utility import COMMANDS as utility
from .ticket import COMMANDS as ticket
from .system import COMMANDS as system
from .advanced import COMMANDS as advanced

PREFIX_COMMAND_GROUPS = {
    "chat": chat, "member": member, "server": server, "moderation": moderation,
    "channel": channel, "role": role, "logs": logs, "security": security,
    "welcome": welcome, "level": level, "games": games, "social": social,
    "math": math, "conversion": conversion, "time": time, "encoding": encoding,
    "utility": utility, "ticket": ticket, "system": system, "advanced": advanced,
}