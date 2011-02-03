#import util.extlogger
import logging
logging.getLogger('')
log = logging.getLogger('wemd')

import util, rc, types, propagators, work_managers, data_manager, pcoords, sim_manager, we_driver, systems
from types import Segment, Particle
from systems import WEMDSystem

version = '0.5'

__all__ = [name for name in dict(locals()) if not name.startswith('_')]
