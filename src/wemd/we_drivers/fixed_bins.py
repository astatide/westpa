import re

import logging
log = logging.getLogger('wemd.we_drivers.fixed_bins')

import numpy
from we_driver import WEDriver
from wemd.core.binarrays import Bin, BinArray
from wemd.core import ConfigError

class FixedBinWEDriver(WEDriver):
    def __init__(self):
        super(FixedBinWEDriver,self).__init__()
        
    def sim_init(self, sim_config, sim_config_src):
        super(FixedBinWEDriver,self).sim_init(sim_config, sim_config_src)
        bintype = sim_config['bins.type']
        assert bintype in ('fixed', 'fixed_bin_particles')

        ndim = sim_config['bins.ndim'] = sim_config_src.get_int('bins.ndim')
        bin_limits = []
        
        if ndim == 1:
            boundary_entries = [key for key in sim_config_src if key.startswith('bins.boundaries')]
            if not boundary_entries:
                raise ConfigError('no bin boundaries provided')
            elif len(boundary_entries) > 1:
                raise ConfigError('more than one bin boundary set provided')
            else:
                boundary_entry = boundary_entries[0]
            
            if boundary_entry in ('bins.boundaries', 'bins.boundaries_0'):
                boundaries = [float(bound) for bound in sim_config_src.get_list(boundary_entry)]
            elif boundary_entry in ('bins.boundaries_expr', 'bins.boundaries_expr_0'):
                boundaries = eval(sim_config_src[boundary_entry])
            else:
                raise ConfigError('invalid bin boundary specification')
                
            bin_limits.append(numpy.array(boundaries))
        else:
            reIsBoundary = re.compile('bins\.boundaries(_expr)?_(\d+)')
            bin_limits = [None] * ndim
            
            boundary_entries = [entry for entry in sim_config_src if entry.startswith('bins.boundaries')]
            for boundary_entry in boundary_entries:
                m = reIsBoundary.match(boundary_entry)
                if not m:
                    raise ConfigError('invalid bin boundary specification')
                else:
                    idim = int(m.group(2))
                    if m.group(1):
                        boundaries = eval(sim_config_src[boundary_entry])
                    else:
                        boundaries = [float(lim) for lim in sim_config_src.get_list(boundary_entry)]
                    bin_limits[idim] = numpy.array(boundaries)
                    
            if None in bin_limits:
                raise ConfigError('missing bin boundaries for at least one dimension')

        #read in the recycling regions
        #[ [lower bound, uppder bound], ...]
        target_pcoords = []
        target_entries = [key for key in sim_config_src if key.startswith('bins.target_pcoord')]
        for target_entry in target_entries:
            target_pcoords.append( eval(sim_config_src.get(target_entry)) )

        #check to make sure they are correct
        for i in xrange(0, len(target_pcoords)):

            if len(target_pcoords[i]) != ndim:
                raise ConfigError("Invalid target pcoord specified, check dimensions %r" % (target_pcoords[i]))

            for idim in xrange(0, len(target_pcoords[i])):
                if len(target_pcoords[i][idim]) != 2:
                    raise ConfigError("Invalid target pcoord specified, [ [lower bound, upper bound], ...] %r" %(target_pcoords[i]))

        if not target_pcoords:
            raise ConfigError("No recycling targets specified")
            
        #read in the source regions
        #[ "Name", [pcoord], [lower bound, upper bound], ...]
        source_pcoords = []
        source_entries = [key for key in sim_config_src if key.startswith('bins.source_pcoord_')]
        reIsSourcePcoord = re.compile('bins\.source_pcoord_(.+)')

        for source_entry in source_entries:
            m = reIsSourcePcoord.match(source_entry)
            if not m:
                raise ConfigError('Invalid source pcoord specified')
            else:
                source_pcoord_vals = eval(sim_config_src.get(source_entry))
                source_pcoord_vals.insert(0,m.group(1))
                source_pcoords.append(source_pcoord_vals)          

        #check to make sure they are correct
        for i in xrange(0, len(source_pcoords)):

            if len(source_pcoords[i]) != ndim+3:
                raise ConfigError("Invalid source pcoord specified, check dimensions \n source_pcoord_%s" % (source_pcoords[i][0]))

            if (source_pcoords[i][1] < 0) or (source_pcoords[i][1] > 1):
                raise ConfigError("Invalid initial weight \n source_pcoord_%s" % (source_pcoords[i][0]))
            
            if len(source_pcoords[i][2]) != ndim:
                raise ConfigError("Invalid source pcoord specified, check pcoord dimensions \n source_pcoord_%s" % (source_pcoords[i][0]))
            
            for idim in xrange(3, len(source_pcoords[i])):
                if len(source_pcoords[i][idim]) != 2:
                    raise ConfigError("Invalid source pcoord specified, [ initial weight, [pcoord],"+\
                                      " [lower bound, upper bound], ...] \n source_pcoord_%s" %(source_pcoords[i][0]))

        sim_config['bin.target_pcoords'] = self.target_pcoords = target_pcoords
        sim_config['bin.source_pcoords'] = self.source_pcoords = source_pcoords
        sim_config['bins.boundaries'] = self.bin_boundaries = bin_limits
        sim_config['bins.particles_per_bin'] = self.particles_per_bin = sim_config_src.get_int('bins.particles_per_bin')       
    
    def make_bins(self):
        return BinArray(boundaries = self.bin_boundaries,
                        ideal_num = self.particles_per_bin)
        # non-time-dependent bin info stored here
