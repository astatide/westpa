#import util.extlogger
import logging
logging.getLogger('')
log = logging.getLogger('wemd')

import util
import _rc
import segment 
from segment import Segment
import propagators, work_managers, data_manager, pcoords, sim_manager, we_driver, systems
from systems import WEMDSystem, InitialState, TargetState

version = '0.6'

rc = _rc._WEMDRC()

__all__ = [name for name in dict(locals()) if not name.startswith('_')]
