"""Completion signals, not build orchestration or arbitrary command execution."""
from .core import Rendezvous, STOP_AFTER_DISPATCH, completion_next
from .policy import Policy
from .herdr import TargetChanged, NotReady, DeliveryUncertain
from .activity import ConnectionActivity
from .table import ActivityTable

__version__ = '0.1.1'
__all__ = ['Rendezvous', 'Policy', 'STOP_AFTER_DISPATCH', 'completion_next',
           'TargetChanged', 'NotReady', 'DeliveryUncertain', 'ConnectionActivity', 'ActivityTable']
