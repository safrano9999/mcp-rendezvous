"""Completion signals, not build orchestration or arbitrary command execution."""
from .core import Rendezvous, STOP_AFTER_DISPATCH, completion_next
from .policy import Policy
from .herdr import TargetChanged, NotReady, DeliveryUncertain

__version__ = '0.1.0'
__all__ = ['Rendezvous', 'Policy', 'STOP_AFTER_DISPATCH', 'completion_next',
           'TargetChanged', 'NotReady', 'DeliveryUncertain']
